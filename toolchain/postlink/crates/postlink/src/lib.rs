//! Whole-program call-graph-scoped `wali.SYS_fork` unwind/rewind rewriter.
//!
//! Preserves WebAssembly 3.0 structured control flow (`block`, `loop`, `if`,
//! `try_table` / `exnref`) while instrumenting *only* the functions that can
//! transitively reach a process-cloning `wali` import (`SYS_fork`, `SYS_clone`,
//! `SYS_clone3`). Modules that do not import a process-cloning syscall pass
//! through with 0% codegen overhead.

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fmt;
use std::fs;
use std::path::PathBuf;

use wasm_encoder::reencode::{self, Reencode};
use wasm_encoder::{
    BlockType as EncBlockType, Catch as EncCatch, CodeSection, CustomSection, ExportKind,
    Function, FunctionSection, Instruction, MemArg, MemArgTarget, Module, NameSection,
    ValType as EncValType,
};
use wasmparser::{
    BinaryReader, BinaryReaderError, BlockType, Catch, CodeSectionReader, CompositeInnerType,
    CustomSectionReader, ElementItems, ExportSectionReader, ExternalKind, FuncType, FunctionBody,
    FunctionSectionReader, Name, NameSectionReader, Operator, Parser, Payload, RefType, TypeRef,
    ValType, Validator, WasmFeatures,
};
use wasmos_abi::syscalls::{lookup_by_import_name, WALI_MODULE};
use wasmos_abi::{kernel_block, TargetHeader, TARGET_SECTION_NAME};

pub const FORK_EXPORTS_SECTION_NAME: &str = "submilli.fork_exports";

#[derive(Debug)]
pub enum PostlinkError {
    Parse(BinaryReaderError),
    Reencode(String),
    InvalidTargetHeader,
    MissingGlobalExport(&'static str),
    InvalidIndex(&'static str),
    UnsupportedForkConstruct(String),
    Validation(BinaryReaderError),
}

impl fmt::Display for PostlinkError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Parse(e) => write!(f, "wasm parse error: {e}"),
            Self::Reencode(msg) => write!(f, "wasm reencode error: {msg}"),
            Self::InvalidTargetHeader => write!(f, "invalid submilli.target custom section"),
            Self::MissingGlobalExport(name) => {
                write!(f, "fork-instrumented module missing required global export `{name}`")
            }
            Self::InvalidIndex(ctx) => write!(f, "out-of-bounds index in {ctx}"),
            Self::UnsupportedForkConstruct(msg) => {
                write!(f, "unsupported Wasm construct in fork-reachable function: {msg}")
            }
            Self::Validation(e) => write!(f, "rewritten Wasm failed validation: {e}"),
        }
    }
}

impl From<BinaryReaderError> for PostlinkError {
    fn from(value: BinaryReaderError) -> Self {
        Self::Parse(value)
    }
}

/// Statistics returned by [`rewrite_module`].
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PostlinkStats {
    pub defined_functions: usize,
    pub fork_reachable_functions: usize,
    pub instrumented: bool,
}

#[derive(Clone, Copy)]
enum KbMode {
    StaticTls {
        tls_base_global: u32,
        kb_offset_global: u32,
    },
    ImportedGlobal(u32),
}

#[derive(Clone, Debug)]
struct ImportedFunc {
    type_idx: u32,
    is_process_fork: bool,
    may_fork_cross_dso: bool,
}

#[derive(Default)]
struct ModuleInfo {
    types: Vec<Option<FuncType>>,
    imported_funcs: Vec<ImportedFunc>,
    defined_func_types: Vec<u32>,
    global_types: Vec<ValType>,
    tls_base_global: Option<u32>,
    kb_offset_global: Option<u32>,
    imported_kb_global: Option<u32>,
    has_dylink: bool,
    has_imported_table: bool,
    defines_libc_start_main: bool,
    exported_funcs: Vec<(String, u32)>,
    dylink_needed: Vec<String>,
    target_header: Option<TargetHeader>,
    address_taken_by_sig: BTreeMap<FuncType, BTreeSet<u32>>,
    direct_callees: Vec<BTreeSet<u32>>,
    indirect_sig_calls: Vec<BTreeSet<FuncType>>,
}

impl ModuleInfo {
    fn num_imported_funcs(&self) -> u32 {
        u32::try_from(self.imported_funcs.len()).unwrap_or(u32::MAX)
    }

    fn func_type(&self, func_idx: u32) -> Result<&FuncType, PostlinkError> {
        let imp_len = self.imported_funcs.len();
        let idx_usize = usize::try_from(func_idx).map_err(|_| PostlinkError::InvalidIndex("func"))?;
        let type_idx = if let Some(imp) = self.imported_funcs.get(idx_usize) {
            imp.type_idx
        } else {
            let def_idx = idx_usize
                .checked_sub(imp_len)
                .ok_or(PostlinkError::InvalidIndex("defined func"))?;
            *self
                .defined_func_types
                .get(def_idx)
                .ok_or(PostlinkError::InvalidIndex("defined func type"))?
        };
        self.type_by_idx(type_idx)
    }

    fn type_by_idx(&self, type_idx: u32) -> Result<&FuncType, PostlinkError> {
        let idx = usize::try_from(type_idx).map_err(|_| PostlinkError::InvalidIndex("type"))?;
        self.types
            .get(idx)
            .and_then(Option::as_ref)
            .ok_or(PostlinkError::InvalidIndex("non-func type"))
    }
}

fn record_addr_taken(info: &mut ModuleInfo, func_idx: u32) -> Result<(), PostlinkError> {
    let sig = info.func_type(func_idx)?.clone();
    info.address_taken_by_sig
        .entry(sig)
        .or_default()
        .insert(func_idx);
    Ok(())
}

fn parse_dylink_needed(data: &[u8]) -> Vec<String> {
    const WASM_DYLINK_NEEDED: u32 = 2;
    let mut needed = Vec::new();
    let mut reader = BinaryReader::new(data, 0);
    while !reader.eof() {
        let Ok(sub_ty) = reader.read_var_u32() else {
            break;
        };
        let Ok(sub_len_u32) = reader.read_var_u32() else {
            break;
        };
        let Ok(sub_len) = usize::try_from(sub_len_u32) else {
            break;
        };
        let Ok(sub_bytes) = reader.read_bytes(sub_len) else {
            break;
        };
        if sub_ty == WASM_DYLINK_NEEDED {
            let mut sub_r = BinaryReader::new(sub_bytes, 0);
            let Ok(count) = sub_r.read_var_u32() else {
                break;
            };
            for _ in 0..count {
                let Ok(str_len_u32) = sub_r.read_var_u32() else {
                    break;
                };
                let Ok(str_len) = usize::try_from(str_len_u32) else {
                    break;
                };
                let Ok(raw) = sub_r.read_bytes(str_len) else {
                    break;
                };
                if let Ok(s) = std::str::from_utf8(raw) {
                    needed.push(s.to_string());
                }
            }
        }
    }
    needed
}

fn extract_exports_from_so(wasm: &[u8]) -> (Vec<String>, Vec<String>, Vec<String>) {
    let mut needed = Vec::new();
    let mut fork_exports = Vec::new();
    let mut all_func_exports = Vec::new();
    for payload in Parser::new(0).parse_all(wasm) {
        match payload {
            Ok(Payload::CustomSection(sec)) => {
                if sec.name() == "dylink.0" {
                    needed.extend(parse_dylink_needed(sec.data()));
                } else if sec.name() == FORK_EXPORTS_SECTION_NAME {
                    if let Ok(text) = std::str::from_utf8(sec.data()) {
                        for line in text.lines() {
                            let trimmed = line.trim();
                            if !trimmed.is_empty() {
                                fork_exports.push(trimmed.to_string());
                            }
                        }
                    }
                }
            }
            Ok(Payload::ExportSection(reader)) => {
                for export in reader.into_iter().flatten() {
                    if matches!(export.kind, ExternalKind::Func | ExternalKind::FuncExact) {
                        all_func_exports.push(export.name.to_string());
                    }
                }
            }
            _ => {}
        }
    }
    (needed, fork_exports, all_func_exports)
}

fn collect_external_fork_symbols(
    initial_needed: &[String],
    lib_dirs: &[PathBuf],
) -> (BTreeSet<String>, BTreeSet<String>) {
    let mut fork_syms: BTreeSet<String> = [
        "fork",
        "_Fork",
        "vfork",
        "popen",
        "system",
        "wordexp",
        "__libc_start_main",
        "__main_argc_argv",
        "__main_void",
        "main",
    ]
    .into_iter()
    .map(str::to_string)
    .collect();
    let mut known_so_exports = BTreeSet::new();

    let mut visited = BTreeSet::new();
    let mut queue: VecDeque<String> = initial_needed.iter().cloned().collect();

    // Also scan `.so` files directly in `lib_dirs` so `-shared` modules built with
    // `ImportDynamic` (e.g. Python C-extensions without explicit `-lpython3.13`)
    // discover `libpython3.13.so`'s fork-capable C-API exports automatically.
    for dir in lib_dirs {
        let Ok(entries) = fs::read_dir(dir) else {
            continue;
        };
        for entry in entries.flatten() {
            let name = entry.file_name();
            if let Some(s) = name.to_str() {
                if s.ends_with(".so") || s.contains(".so.") {
                    queue.push_back(s.to_string());
                }
            }
        }
    }

    while let Some(so_name) = queue.pop_front() {
        if !visited.insert(so_name.clone()) {
            continue;
        }
        for dir in lib_dirs {
            let cand = dir.join(&so_name);
            let Ok(bytes) = fs::read(&cand) else {
                continue;
            };
            if bytes.get(..4) != Some(b"\0asm") {
                continue;
            }
            let (more_needed, f_exports, func_exports) = extract_exports_from_so(&bytes);
            fork_syms.extend(f_exports);
            known_so_exports.extend(func_exports);
            for dep in more_needed {
                if !visited.contains(&dep) {
                    queue.push_back(dep);
                }
            }
            break;
        }
    }
    (fork_syms, known_so_exports)
}

fn peek_dylink_info(wasm: &[u8]) -> (bool, Vec<String>) {
    for payload in Parser::new(0).parse_all(wasm) {
        match payload {
            Ok(Payload::CustomSection(sec)) if sec.name() == "dylink.0" => {
                return (true, parse_dylink_needed(sec.data()));
            }
            Ok(Payload::TypeSection(_)) | Err(_) => break,
            _ => {}
        }
    }
    (false, Vec::new())
}

fn analyze_module(
    wasm: &[u8],
    has_dylink_hint: bool,
    external_fork_syms: &BTreeSet<String>,
    known_so_exports: &BTreeSet<String>,
) -> Result<ModuleInfo, PostlinkError> {
    let mut info = ModuleInfo::default();
    let mut elem_funcs: Vec<u32> = Vec::new();
    let mut code_bodies: Vec<FunctionBody<'_>> = Vec::new();

    for payload in Parser::new(0).parse_all(wasm) {
        match payload? {
            Payload::TypeSection(reader) => {
                for rec_group in reader {
                    let rec_group = rec_group?;
                    for sub_ty in rec_group.into_types() {
                        match sub_ty.composite_type.inner {
                            CompositeInnerType::Func(func_ty) => info.types.push(Some(func_ty)),
                            _ => info.types.push(None),
                        }
                    }
                }
            }
            Payload::ImportSection(reader) => {
                for import in reader.into_imports() {
                    let import = import?;
                    match import.ty {
                        TypeRef::Func(type_idx) | TypeRef::FuncExact(type_idx) => {
                            let is_process_fork = import.module == WALI_MODULE
                                && lookup_by_import_name(import.name)
                                    .is_some_and(|spec| spec.is_process_fork);
                            let may_fork_cross_dso = !is_process_fork
                                && import.module == "env"
                                && (external_fork_syms.contains(import.name)
                                    || (has_dylink_hint && !known_so_exports.contains(import.name)));
                            info.imported_funcs.push(ImportedFunc {
                                type_idx,
                                is_process_fork,
                                may_fork_cross_dso,
                            });
                        }
                        TypeRef::Global(g) => {
                            let idx_u32 = u32::try_from(info.global_types.len())
                                .map_err(|_| PostlinkError::InvalidIndex("global import"))?;
                            if import.module == "submilli" && import.name == "__kernel_block" {
                                info.imported_kb_global = Some(idx_u32);
                            }
                            info.global_types.push(g.content_type);
                        }
                        TypeRef::Table(_) => {
                            info.has_imported_table = true;
                        }
                        _ => {}
                    }
                }
            }
            Payload::FunctionSection(reader) => {
                for ty in reader {
                    info.defined_func_types.push(ty?);
                }
            }
            Payload::GlobalSection(reader) => {
                for global in reader {
                    let global = global?;
                    info.global_types.push(global.ty.content_type);
                    let mut ops = global.init_expr.get_operators_reader();
                    while !ops.eof() {
                        if let Operator::RefFunc { function_index } = ops.read()? {
                            elem_funcs.push(function_index);
                        }
                    }
                }
            }
            Payload::ExportSection(reader) => {
                process_exports(&mut info, reader)?;
            }
            Payload::ElementSection(reader) => {
                for elem in reader {
                    let elem = elem?;
                    match elem.items {
                        ElementItems::Functions(funcs) => {
                            for f in funcs {
                                elem_funcs.push(f?);
                            }
                        }
                        ElementItems::Expressions(_, exprs) => {
                            for expr in exprs {
                                let expr = expr?;
                                let mut ops = expr.get_operators_reader();
                                while !ops.eof() {
                                    if let Operator::RefFunc { function_index } = ops.read()? {
                                        elem_funcs.push(function_index);
                                    }
                                }
                            }
                        }
                    }
                }
            }
            Payload::CodeSectionEntry(body) => code_bodies.push(body),
            Payload::CustomSection(sec) if sec.name() == "dylink.0" => {
                info.has_dylink = true;
                info.dylink_needed = parse_dylink_needed(sec.data());
            }
            Payload::CustomSection(sec) if sec.name() == TARGET_SECTION_NAME => {
                let hdr =
                    TargetHeader::decode(sec.data()).map_err(|_| PostlinkError::InvalidTargetHeader)?;
                info.target_header = Some(hdr);
            }
            _ => {}
        }
    }

    for f in elem_funcs {
        record_addr_taken(&mut info, f)?;
    }

    for body in &code_bodies {
        let mut callees = BTreeSet::new();
        let mut indirect_sigs = BTreeSet::new();
        let mut ops = body.get_operators_reader()?;
        while !ops.eof() {
            match ops.read()? {
                Operator::Call { function_index } | Operator::ReturnCall { function_index } => {
                    callees.insert(function_index);
                }
                Operator::CallIndirect { type_index, .. }
                | Operator::ReturnCallIndirect { type_index, .. } => {
                    let sig = info.type_by_idx(type_index)?.clone();
                    indirect_sigs.insert(sig);
                }
                Operator::RefFunc { function_index } => {
                    record_addr_taken(&mut info, function_index)?;
                }
                _ => {}
            }
        }
        info.direct_callees.push(callees);
        info.indirect_sig_calls.push(indirect_sigs);
    }

    Ok(info)
}

fn process_exports(
    info: &mut ModuleInfo,
    reader: ExportSectionReader<'_>,
) -> Result<(), PostlinkError> {
    for export in reader {
        let export = export?;
        match export.kind {
            ExternalKind::Global => {
                if export.name == "__tls_base" {
                    info.tls_base_global = Some(export.index);
                } else if export.name == "__submilli_kernel_block" {
                    info.kb_offset_global = Some(export.index);
                }
            }
            ExternalKind::Func | ExternalKind::FuncExact => {
                if export.name == "__libc_start_main" {
                    info.defines_libc_start_main = true;
                }
                info.exported_funcs
                    .push((export.name.to_string(), export.index));
            }
            _ => {}
        }
    }
    Ok(())
}

/// Compute the set of all function indices (both imported fork syscalls and
/// defined functions) that can transitively invoke a process-forking import.
fn compute_fork_reachable(info: &ModuleInfo) -> Result<(BTreeSet<u32>, BTreeSet<FuncType>), PostlinkError> {
    let mut fork_funcs = BTreeSet::new();
    for (idx, imp) in info.imported_funcs.iter().enumerate() {
        if imp.is_process_fork || imp.may_fork_cross_dso {
            let idx_u32 = u32::try_from(idx).map_err(|_| PostlinkError::InvalidIndex("import"))?;
            fork_funcs.insert(idx_u32);
        }
    }

    let mut extra_indirect_sigs = BTreeSet::new();
    if info.has_dylink && info.defines_libc_start_main {
        // In `libc.so`, `libc_start_main_stage2` calls the PIE's `main(int, char**, char**)`
        // via function pointer; mark that indirect signature fork-capable so `libc_start_main`
        // unwinds cleanly whenever the PIE's `main` calls `fork()`.
        let main_sig = FuncType::new([ValType::I32, ValType::I64, ValType::I64], [ValType::I32]);
        extra_indirect_sigs.insert(main_sig);
    }

    if fork_funcs.is_empty() && extra_indirect_sigs.is_empty() {
        return Ok((fork_funcs, BTreeSet::new()));
    }

    // Build reverse call graph among all functions.
    let imp_count = info.imported_funcs.len();
    let mut reverse_edges: BTreeMap<u32, BTreeSet<u32>> = BTreeMap::new();
    for (def_idx, callees) in info.direct_callees.iter().enumerate() {
        let caller = u32::try_from(
            imp_count
                .checked_add(def_idx)
                .ok_or(PostlinkError::InvalidIndex("caller"))?,
        )
        .map_err(|_| PostlinkError::InvalidIndex("caller u32"))?;

        for &callee in callees {
            reverse_edges.entry(callee).or_default().insert(caller);
        }
        if let Some(sigs) = info.indirect_sig_calls.get(def_idx) {
            for sig in sigs {
                if extra_indirect_sigs.contains(sig) {
                    fork_funcs.insert(caller);
                }
                if let Some(targets) = info.address_taken_by_sig.get(sig) {
                    for &target in targets {
                        reverse_edges.entry(target).or_default().insert(caller);
                    }
                }
            }
        }
    }

    let mut queue: VecDeque<u32> = fork_funcs.iter().copied().collect();
    while let Some(callee) = queue.pop_front() {
        if let Some(callers) = reverse_edges.get(&callee) {
            for &caller in callers {
                if fork_funcs.insert(caller) {
                    queue.push_back(caller);
                }
            }
        }
    }

    let mut fork_indirect_sigs = extra_indirect_sigs;
    for (sig, targets) in &info.address_taken_by_sig {
        if targets.iter().any(|f| fork_funcs.contains(f)) {
            fork_indirect_sigs.insert(sig.clone());
        }
    }

    Ok((fork_funcs, fork_indirect_sigs))
}

#[derive(Clone, Debug)]
enum AstNode<'a> {
    Instr {
        op: Operator<'a>,
        fork_site: Option<u32>,
        in_stack: Vec<ValType>,
        out_stack: Vec<ValType>,
        call_params: Vec<ValType>,
        call_results: Vec<ValType>,
        is_indirect: bool,
    },
    Block {
        blockty: BlockType,
        body: Vec<AstNode<'a>>,
        fork_range: Option<(u32, u32)>,
        in_stack: Vec<ValType>,
        out_stack: Vec<ValType>,
        params: Vec<ValType>,
        results: Vec<ValType>,
        body_end_polymorphic: bool,
    },
    Loop {
        blockty: BlockType,
        body: Vec<AstNode<'a>>,
        fork_range: Option<(u32, u32)>,
        in_stack: Vec<ValType>,
        out_stack: Vec<ValType>,
        params: Vec<ValType>,
        results: Vec<ValType>,
        body_end_polymorphic: bool,
    },
    If {
        blockty: BlockType,
        then_body: Vec<AstNode<'a>>,
        else_body: Vec<AstNode<'a>>,
        has_else: bool,
        then_range: Option<(u32, u32)>,
        else_range: Option<(u32, u32)>,
        fork_range: Option<(u32, u32)>,
        in_stack: Vec<ValType>,
        out_stack: Vec<ValType>,
        params: Vec<ValType>,
        results: Vec<ValType>,
        then_end_polymorphic: bool,
        else_end_polymorphic: bool,
    },
    TryTable {
        blockty: BlockType,
        catches: Vec<Catch>,
        body: Vec<AstNode<'a>>,
        fork_range: Option<(u32, u32)>,
        in_stack: Vec<ValType>,
        out_stack: Vec<ValType>,
        params: Vec<ValType>,
        results: Vec<ValType>,
        body_end_polymorphic: bool,
    },
}

impl AstNode<'_> {
    fn fork_range(&self) -> Option<(u32, u32)> {
        match self {
            Self::Instr { fork_site, .. } => fork_site.map(|id| (id, id)),
            Self::Block { fork_range, .. }
            | Self::Loop { fork_range, .. }
            | Self::If { fork_range, .. }
            | Self::TryTable { fork_range, .. } => *fork_range,
        }
    }

    fn in_stack(&self) -> &[ValType] {
        match self {
            Self::Instr { in_stack, .. }
            | Self::Block { in_stack, .. }
            | Self::Loop { in_stack, .. }
            | Self::If { in_stack, .. }
            | Self::TryTable { in_stack, .. } => in_stack,
        }
    }

    fn out_stack(&self) -> &[ValType] {
        match self {
            Self::Instr { out_stack, .. }
            | Self::Block { out_stack, .. }
            | Self::Loop { out_stack, .. }
            | Self::If { out_stack, .. }
            | Self::TryTable { out_stack, .. } => out_stack,
        }
    }
}

fn merge_ranges(a: Option<(u32, u32)>, b: Option<(u32, u32)>) -> Option<(u32, u32)> {
    match (a, b) {
        (None, x) | (x, None) => x,
        (Some((l1, h1)), Some((l2, h2))) => Some((l1.min(l2), h1.max(h2))),
    }
}

fn list_fork_range(nodes: &[AstNode<'_>]) -> Option<(u32, u32)> {
    nodes
        .iter()
        .fold(None, |acc, n| merge_ranges(acc, n.fork_range()))
}

fn block_sig(info: &ModuleInfo, blockty: BlockType) -> Result<(Vec<ValType>, Vec<ValType>), PostlinkError> {
    match blockty {
        BlockType::Empty => Ok((Vec::new(), Vec::new())),
        BlockType::Type(t) => Ok((Vec::new(), vec![t])),
        BlockType::FuncType(idx) => {
            let fty = info.type_by_idx(idx)?;
            Ok((fty.params().to_vec(), fty.results().to_vec()))
        }
    }
}

fn is_savable_scalar(ty: ValType) -> bool {
    matches!(
        ty,
        ValType::I32 | ValType::I64 | ValType::F32 | ValType::F64 | ValType::V128
    )
}

fn scalar_slot_bytes(ty: ValType) -> Result<u64, PostlinkError> {
    match ty {
        ValType::I32 | ValType::I64 | ValType::F32 | ValType::F64 => Ok(8),
        ValType::V128 => Ok(16),
        ValType::Ref(_) => Err(PostlinkError::UnsupportedForkConstruct(
            "reference value live across fork call".into(),
        )),
    }
}

struct TypeTracker<'m> {
    info: &'m ModuleInfo,
    locals: Vec<ValType>,
    fork_callee_map: &'m BTreeMap<u32, u32>,
    fork_funcs: &'m BTreeSet<u32>,
    fork_indirect_sigs: &'m BTreeSet<FuncType>,
    next_site_id: u32,
}

#[derive(Clone)]
struct ScopeState {
    stack: Vec<ValType>,
    polymorphic: bool,
}

impl ScopeState {
    fn new(params: Vec<ValType>) -> Self {
        Self {
            stack: params,
            polymorphic: false,
        }
    }

    fn pop(&mut self) -> Result<Option<ValType>, PostlinkError> {
        if let Some(v) = self.stack.pop() {
            Ok(Some(v))
        } else if self.polymorphic {
            Ok(None)
        } else {
            Err(PostlinkError::UnsupportedForkConstruct(
                "operand stack underflow in fork-reachable function".into(),
            ))
        }
    }

    fn pop_n(&mut self, n: usize) -> Result<(), PostlinkError> {
        for _ in 0..n {
            let _ = self.pop()?;
        }
        Ok(())
    }

    fn push(&mut self, ty: ValType) {
        if !self.polymorphic {
            self.stack.push(ty);
        }
    }

    fn push_slice(&mut self, tys: &[ValType]) {
        if !self.polymorphic {
            self.stack.extend_from_slice(tys);
        }
    }
}

impl<'m> TypeTracker<'m> {
    fn parse_body(&mut self, ops: &[Operator<'m>]) -> Result<(Vec<AstNode<'m>>, bool), PostlinkError> {
        let mut pos = 0_usize;
        let func_state = ScopeState::new(Vec::new());
        let (nodes, end_poly, next_pos, saw_else) = self.parse_seq(ops, &mut pos, func_state, false)?;
        if saw_else || next_pos != ops.len() {
            return Err(PostlinkError::UnsupportedForkConstruct(
                "unexpected trailing operators after function end".into(),
            ));
        }
        Ok((nodes, end_poly))
    }

    fn alloc_site(&mut self) -> Result<u32, PostlinkError> {
        let id = self.next_site_id;
        self.next_site_id = self
            .next_site_id
            .checked_add(1)
            .ok_or(PostlinkError::InvalidIndex("fork site id overflow"))?;
        Ok(id)
    }

    fn parse_seq(
        &mut self,
        ops: &[Operator<'m>],
        pos: &mut usize,
        mut state: ScopeState,
        allow_else: bool,
    ) -> Result<(Vec<AstNode<'m>>, bool, usize, bool), PostlinkError> {
        let mut out = Vec::new();
        while let Some(op) = ops.get(*pos) {
            *pos = pos
                .checked_add(1)
                .ok_or(PostlinkError::InvalidIndex("op cursor"))?;

            match op {
                Operator::End => {
                    return Ok((out, state.polymorphic, *pos, false));
                }
                Operator::Else if allow_else => {
                    return Ok((out, state.polymorphic, *pos, true));
                }
                Operator::Block { blockty } => {
                    let (params, results) = block_sig(self.info, *blockty)?;
                    let in_stack = state.stack.clone();
                    state.pop_n(params.len())?;
                    let inner_state = ScopeState::new(params.clone());
                    let (body, body_end_poly, _, _) =
                        self.parse_seq(ops, pos, inner_state, false)?;
                    let fork_range = list_fork_range(&body);
                    state.push_slice(&results);
                    let out_stack = state.stack.clone();
                    out.push(AstNode::Block {
                        blockty: *blockty,
                        body,
                        fork_range,
                        in_stack,
                        out_stack,
                        params,
                        results,
                        body_end_polymorphic: body_end_poly,
                    });
                }
                Operator::Loop { blockty } => {
                    let (params, results) = block_sig(self.info, *blockty)?;
                    let in_stack = state.stack.clone();
                    state.pop_n(params.len())?;
                    let inner_state = ScopeState::new(params.clone());
                    let (body, body_end_poly, _, _) =
                        self.parse_seq(ops, pos, inner_state, false)?;
                    let fork_range = list_fork_range(&body);
                    state.push_slice(&results);
                    let out_stack = state.stack.clone();
                    out.push(AstNode::Loop {
                        blockty: *blockty,
                        body,
                        fork_range,
                        in_stack,
                        out_stack,
                        params,
                        results,
                        body_end_polymorphic: body_end_poly,
                    });
                }
                Operator::If { blockty } => {
                    let (params, results) = block_sig(self.info, *blockty)?;
                    let in_stack = state.stack.clone();
                    let _ = state.pop()?; // condition i32
                    state.pop_n(params.len())?;
                    let then_init = ScopeState::new(params.clone());
                    let (then_body, then_end_poly, _, saw_else) =
                        self.parse_seq(ops, pos, then_init, true)?;
                    let (else_body, else_end_poly, has_else) = if saw_else {
                        let else_init = ScopeState::new(params.clone());
                        let (eb, ep, _, _) = self.parse_seq(ops, pos, else_init, false)?;
                        (eb, ep, true)
                    } else {
                        (Vec::new(), false, false)
                    };
                    let then_range = list_fork_range(&then_body);
                    let else_range = list_fork_range(&else_body);
                    let fork_range = merge_ranges(then_range, else_range);
                    state.push_slice(&results);
                    let out_stack = state.stack.clone();
                    out.push(AstNode::If {
                        blockty: *blockty,
                        then_body,
                        else_body,
                        has_else,
                        then_range,
                        else_range,
                        fork_range,
                        in_stack,
                        out_stack,
                        params,
                        results,
                        then_end_polymorphic: then_end_poly,
                        else_end_polymorphic: else_end_poly,
                    });
                }
                Operator::TryTable { try_table } => {
                    let (params, results) = block_sig(self.info, try_table.ty)?;
                    let in_stack = state.stack.clone();
                    state.pop_n(params.len())?;
                    let inner_state = ScopeState::new(params.clone());
                    let (body, body_end_poly, _, _) =
                        self.parse_seq(ops, pos, inner_state, false)?;
                    let fork_range = list_fork_range(&body);
                    state.push_slice(&results);
                    let out_stack = state.stack.clone();
                    out.push(AstNode::TryTable {
                        blockty: try_table.ty,
                        catches: try_table.catches.clone(),
                        body,
                        fork_range,
                        in_stack,
                        out_stack,
                        params,
                        results,
                        body_end_polymorphic: body_end_poly,
                    });
                }
                Operator::ReturnCall { function_index }
                    if !state.polymorphic && self.fork_funcs.contains(function_index) =>
                {
                    // Lower tail-call to fork-reachable function into Call + Return.
                    let mapped_callee = self
                        .fork_callee_map
                        .get(function_index)
                        .copied()
                        .unwrap_or(*function_index);
                    let fty = self.info.func_type(*function_index)?.clone();
                    let in_stack = state.stack.clone();
                    state.pop_n(fty.params().len())?;
                    state.push_slice(fty.results());
                    let out_stack = state.stack.clone();
                    let site = self.alloc_site()?;
                    out.push(AstNode::Instr {
                        op: Operator::Call {
                            function_index: mapped_callee,
                        },
                        fork_site: Some(site),
                        in_stack,
                        out_stack: out_stack.clone(),
                        call_params: fty.params().to_vec(),
                        call_results: fty.results().to_vec(),
                        is_indirect: false,
                    });
                    state.polymorphic = true;
                    state.stack.clear();
                    out.push(AstNode::Instr {
                        op: Operator::Return,
                        fork_site: None,
                        in_stack: out_stack,
                        out_stack: Vec::new(),
                        call_params: Vec::new(),
                        call_results: Vec::new(),
                        is_indirect: false,
                    });
                }
                Operator::ReturnCallIndirect {
                    type_index,
                    table_index,
                } if !state.polymorphic => {
                    let fty = self.info.type_by_idx(*type_index)?.clone();
                    if self.fork_indirect_sigs.contains(&fty) {
                        let in_stack = state.stack.clone();
                        let _ = state.pop()?; // table idx
                        state.pop_n(fty.params().len())?;
                        state.push_slice(fty.results());
                        let out_stack = state.stack.clone();
                        let site = self.alloc_site()?;
                        out.push(AstNode::Instr {
                            op: Operator::CallIndirect {
                                type_index: *type_index,
                                table_index: *table_index,
                            },
                            fork_site: Some(site),
                            in_stack,
                            out_stack: out_stack.clone(),
                            call_params: fty.params().to_vec(),
                            call_results: fty.results().to_vec(),
                            is_indirect: true,
                        });
                        state.polymorphic = true;
                        state.stack.clear();
                        out.push(AstNode::Instr {
                            op: Operator::Return,
                            fork_site: None,
                            in_stack: out_stack,
                            out_stack: Vec::new(),
                            call_params: Vec::new(),
                            call_results: Vec::new(),
                            is_indirect: false,
                        });
                    } else {
                        let in_stack = state.stack.clone();
                        state.polymorphic = true;
                        state.stack.clear();
                        out.push(AstNode::Instr {
                            op: op.clone(),
                            fork_site: None,
                            in_stack,
                            out_stack: Vec::new(),
                            call_params: Vec::new(),
                            call_results: Vec::new(),
                            is_indirect: false,
                        });
                    }
                }
                Operator::Call { function_index } => {
                    let in_stack = state.stack.clone();
                    let fty = self.info.func_type(*function_index)?.clone();
                    let is_fork = !state.polymorphic && self.fork_funcs.contains(function_index);
                    let mapped_callee = self
                        .fork_callee_map
                        .get(function_index)
                        .copied()
                        .unwrap_or(*function_index);
                    state.pop_n(fty.params().len())?;
                    state.push_slice(fty.results());
                    let out_stack = state.stack.clone();
                    let fork_site = if is_fork {
                        Some(self.alloc_site()?)
                    } else {
                        None
                    };
                    out.push(AstNode::Instr {
                        op: Operator::Call {
                            function_index: mapped_callee,
                        },
                        fork_site,
                        in_stack,
                        out_stack,
                        call_params: fty.params().to_vec(),
                        call_results: fty.results().to_vec(),
                        is_indirect: false,
                    });
                }
                Operator::CallIndirect {
                    type_index,
                    table_index,
                } => {
                    let in_stack = state.stack.clone();
                    let fty = self.info.type_by_idx(*type_index)?.clone();
                    let is_fork = !state.polymorphic && self.fork_indirect_sigs.contains(&fty);
                    let _ = state.pop()?; // table index (i32 or i64)
                    state.pop_n(fty.params().len())?;
                    state.push_slice(fty.results());
                    let out_stack = state.stack.clone();
                    let fork_site = if is_fork {
                        Some(self.alloc_site()?)
                    } else {
                        None
                    };
                    out.push(AstNode::Instr {
                        op: Operator::CallIndirect {
                            type_index: *type_index,
                            table_index: *table_index,
                        },
                        fork_site,
                        in_stack,
                        out_stack,
                        call_params: fty.params().to_vec(),
                        call_results: fty.results().to_vec(),
                        is_indirect: true,
                    });
                }
                other => {
                    let in_stack = state.stack.clone();
                    self.apply_simple_op(other, &mut state)?;
                    let out_stack = state.stack.clone();
                    out.push(AstNode::Instr {
                        op: other.clone(),
                        fork_site: None,
                        in_stack,
                        out_stack,
                        call_params: Vec::new(),
                        call_results: Vec::new(),
                        is_indirect: false,
                    });
                }
            }
        }
        Err(PostlinkError::UnsupportedForkConstruct(
            "unterminated block in fork-reachable function".into(),
        ))
    }

    #[allow(clippy::too_many_lines)]
    fn apply_simple_op(
        &self,
        op: &Operator<'_>,
        state: &mut ScopeState,
    ) -> Result<(), PostlinkError> {
        match op {
            Operator::Unreachable
            | Operator::Return
            | Operator::Br { .. }
            | Operator::BrTable { .. }
            | Operator::ReturnCall { .. }
            | Operator::ReturnCallIndirect { .. }
            | Operator::Throw { .. }
            | Operator::ThrowRef => {
                state.polymorphic = true;
                state.stack.clear();
            }
            Operator::Nop | Operator::DataDrop { .. } | Operator::ElemDrop { .. } => {}
            Operator::BrIf { .. } => {
                let _ = state.pop()?;
            }
            Operator::Drop => {
                let _ = state.pop()?;
            }
            Operator::Select => {
                let _ = state.pop()?; // cond i32
                let b = state.pop()?;
                let a = state.pop()?;
                if let Some(ty) = a.or(b) {
                    state.push(ty);
                }
            }
            Operator::TypedSelect { ty } => {
                state.pop_n(3)?;
                state.push(*ty);
            }
            Operator::LocalGet { local_index } => {
                let idx = usize::try_from(*local_index)
                    .map_err(|_| PostlinkError::InvalidIndex("local_get"))?;
                let ty = *self
                    .locals
                    .get(idx)
                    .ok_or(PostlinkError::InvalidIndex("local_get bounds"))?;
                state.push(ty);
            }
            Operator::LocalSet { .. } => {
                let _ = state.pop()?;
            }
            Operator::LocalTee { local_index } => {
                let _ = state.pop()?;
                let idx = usize::try_from(*local_index)
                    .map_err(|_| PostlinkError::InvalidIndex("local_tee"))?;
                let ty = *self
                    .locals
                    .get(idx)
                    .ok_or(PostlinkError::InvalidIndex("local_tee bounds"))?;
                state.push(ty);
            }
            Operator::GlobalGet { global_index } => {
                let idx = usize::try_from(*global_index)
                    .map_err(|_| PostlinkError::InvalidIndex("global_get"))?;
                let ty = *self
                    .info
                    .global_types
                    .get(idx)
                    .ok_or(PostlinkError::InvalidIndex("global_get bounds"))?;
                state.push(ty);
            }
            Operator::GlobalSet { .. } => {
                let _ = state.pop()?;
            }
            Operator::I32Const { .. } => state.push(ValType::I32),
            Operator::I64Const { .. } => state.push(ValType::I64),
            Operator::F32Const { .. } => state.push(ValType::F32),
            Operator::F64Const { .. } => state.push(ValType::F64),
            Operator::V128Const { .. } => state.push(ValType::V128),
            Operator::RefNull { hty } => {
                let rty = RefType::new(true, *hty)
                    .ok_or_else(|| PostlinkError::UnsupportedForkConstruct("RefNull".into()))?;
                state.push(ValType::Ref(rty));
            }
            Operator::RefFunc { .. } => state.push(ValType::Ref(RefType::FUNCREF)),
            Operator::RefIsNull => {
                let _ = state.pop()?;
                state.push(ValType::I32);
            }
            Operator::I32Load { .. }
            | Operator::I32Load8S { .. }
            | Operator::I32Load8U { .. }
            | Operator::I32Load16S { .. }
            | Operator::I32Load16U { .. } => {
                let _ = state.pop()?;
                state.push(ValType::I32);
            }
            Operator::I64Load { .. }
            | Operator::I64Load8S { .. }
            | Operator::I64Load8U { .. }
            | Operator::I64Load16S { .. }
            | Operator::I64Load16U { .. }
            | Operator::I64Load32S { .. }
            | Operator::I64Load32U { .. } => {
                let _ = state.pop()?;
                state.push(ValType::I64);
            }
            Operator::F32Load { .. } => {
                let _ = state.pop()?;
                state.push(ValType::F32);
            }
            Operator::F64Load { .. } => {
                let _ = state.pop()?;
                state.push(ValType::F64);
            }
            Operator::V128Load { .. }
            | Operator::V128Load8x8S { .. }
            | Operator::V128Load8x8U { .. }
            | Operator::V128Load16x4S { .. }
            | Operator::V128Load16x4U { .. }
            | Operator::V128Load32x2S { .. }
            | Operator::V128Load32x2U { .. }
            | Operator::V128Load8Splat { .. }
            | Operator::V128Load16Splat { .. }
            | Operator::V128Load32Splat { .. }
            | Operator::V128Load64Splat { .. }
            | Operator::V128Load32Zero { .. }
            | Operator::V128Load64Zero { .. } => {
                let _ = state.pop()?;
                state.push(ValType::V128);
            }
            Operator::V128Load8Lane { .. }
            | Operator::V128Load16Lane { .. }
            | Operator::V128Load32Lane { .. }
            | Operator::V128Load64Lane { .. } => {
                state.pop_n(2)?;
                state.push(ValType::V128);
            }
            Operator::I32Store { .. }
            | Operator::I32Store8 { .. }
            | Operator::I32Store16 { .. }
            | Operator::I64Store { .. }
            | Operator::I64Store8 { .. }
            | Operator::I64Store16 { .. }
            | Operator::I64Store32 { .. }
            | Operator::F32Store { .. }
            | Operator::F64Store { .. }
            | Operator::V128Store { .. }
            | Operator::V128Store8Lane { .. }
            | Operator::V128Store16Lane { .. }
            | Operator::V128Store32Lane { .. }
            | Operator::V128Store64Lane { .. } => {
                state.pop_n(2)?;
            }
            Operator::MemorySize { .. } => {
                // memory64 uses i64 page counts
                state.push(ValType::I64);
            }
            Operator::MemoryGrow { .. } => {
                let ptr_ty = state.pop()?.unwrap_or(ValType::I64);
                state.push(ptr_ty);
            }
            Operator::MemoryCopy { .. }
            | Operator::MemoryFill { .. }
            | Operator::MemoryInit { .. } => {
                state.pop_n(3)?;
            }
            Operator::TableGet { .. } => {
                let _ = state.pop()?;
                state.push(ValType::Ref(RefType::FUNCREF));
            }
            Operator::TableSet { .. } => {
                state.pop_n(2)?;
            }
            Operator::I32Eqz
            | Operator::I64Eqz
            | Operator::I32WrapI64
            | Operator::I32TruncF32S
            | Operator::I32TruncF32U
            | Operator::I32TruncF64S
            | Operator::I32TruncF64U
            | Operator::I32TruncSatF32S
            | Operator::I32TruncSatF32U
            | Operator::I32TruncSatF64S
            | Operator::I32TruncSatF64U
            | Operator::I32ReinterpretF32
            | Operator::I32Clz
            | Operator::I32Ctz
            | Operator::I32Popcnt
            | Operator::I32Extend8S
            | Operator::I32Extend16S => {
                let _ = state.pop()?;
                state.push(ValType::I32);
            }
            Operator::I64ExtendI32S
            | Operator::I64ExtendI32U
            | Operator::I64TruncF32S
            | Operator::I64TruncF32U
            | Operator::I64TruncF64S
            | Operator::I64TruncF64U
            | Operator::I64TruncSatF32S
            | Operator::I64TruncSatF32U
            | Operator::I64TruncSatF64S
            | Operator::I64TruncSatF64U
            | Operator::I64ReinterpretF64
            | Operator::I64Clz
            | Operator::I64Ctz
            | Operator::I64Popcnt
            | Operator::I64Extend8S
            | Operator::I64Extend16S
            | Operator::I64Extend32S => {
                let _ = state.pop()?;
                state.push(ValType::I64);
            }
            Operator::F32ConvertI32S
            | Operator::F32ConvertI32U
            | Operator::F32ConvertI64S
            | Operator::F32ConvertI64U
            | Operator::F32DemoteF64
            | Operator::F32ReinterpretI32
            | Operator::F32Abs
            | Operator::F32Neg
            | Operator::F32Ceil
            | Operator::F32Floor
            | Operator::F32Trunc
            | Operator::F32Nearest
            | Operator::F32Sqrt => {
                let _ = state.pop()?;
                state.push(ValType::F32);
            }
            Operator::F64ConvertI32S
            | Operator::F64ConvertI32U
            | Operator::F64ConvertI64S
            | Operator::F64ConvertI64U
            | Operator::F64PromoteF32
            | Operator::F64ReinterpretI64
            | Operator::F64Abs
            | Operator::F64Neg
            | Operator::F64Ceil
            | Operator::F64Floor
            | Operator::F64Trunc
            | Operator::F64Nearest
            | Operator::F64Sqrt => {
                let _ = state.pop()?;
                state.push(ValType::F64);
            }
            Operator::I32Eq
            | Operator::I32Ne
            | Operator::I32LtS
            | Operator::I32LtU
            | Operator::I32GtS
            | Operator::I32GtU
            | Operator::I32LeS
            | Operator::I32LeU
            | Operator::I32GeS
            | Operator::I32GeU
            | Operator::I64Eq
            | Operator::I64Ne
            | Operator::I64LtS
            | Operator::I64LtU
            | Operator::I64GtS
            | Operator::I64GtU
            | Operator::I64LeS
            | Operator::I64LeU
            | Operator::I64GeS
            | Operator::I64GeU
            | Operator::F32Eq
            | Operator::F32Ne
            | Operator::F32Lt
            | Operator::F32Gt
            | Operator::F32Le
            | Operator::F32Ge
            | Operator::F64Eq
            | Operator::F64Ne
            | Operator::F64Lt
            | Operator::F64Gt
            | Operator::F64Le
            | Operator::F64Ge
            | Operator::I32Add
            | Operator::I32Sub
            | Operator::I32Mul
            | Operator::I32DivS
            | Operator::I32DivU
            | Operator::I32RemS
            | Operator::I32RemU
            | Operator::I32And
            | Operator::I32Or
            | Operator::I32Xor
            | Operator::I32Shl
            | Operator::I32ShrS
            | Operator::I32ShrU
            | Operator::I32Rotl
            | Operator::I32Rotr => {
                state.pop_n(2)?;
                state.push(ValType::I32);
            }
            Operator::I64Add
            | Operator::I64Sub
            | Operator::I64Mul
            | Operator::I64DivS
            | Operator::I64DivU
            | Operator::I64RemS
            | Operator::I64RemU
            | Operator::I64And
            | Operator::I64Or
            | Operator::I64Xor
            | Operator::I64Shl
            | Operator::I64ShrS
            | Operator::I64ShrU
            | Operator::I64Rotl
            | Operator::I64Rotr => {
                state.pop_n(2)?;
                state.push(ValType::I64);
            }
            Operator::F32Add
            | Operator::F32Sub
            | Operator::F32Mul
            | Operator::F32Div
            | Operator::F32Min
            | Operator::F32Max
            | Operator::F32Copysign => {
                state.pop_n(2)?;
                state.push(ValType::F32);
            }
            Operator::F64Add
            | Operator::F64Sub
            | Operator::F64Mul
            | Operator::F64Div
            | Operator::F64Min
            | Operator::F64Max
            | Operator::F64Copysign => {
                state.pop_n(2)?;
                state.push(ValType::F64);
            }
            simd => self.apply_simd_op(simd, state)?,
        }
        Ok(())
    }

    fn apply_simd_op(
        &self,
        op: &Operator<'_>,
        state: &mut ScopeState,
    ) -> Result<(), PostlinkError> {
        let name = format!("{op:?}");
        if name.starts_with("V128Bitselect") {
            state.pop_n(3)?;
            state.push(ValType::V128);
        } else if name.contains("Splat")
            || name.starts_with("V128Not")
            || name.contains("Abs")
            || name.contains("Neg")
            || name.contains("Sqrt")
            || name.contains("Ceil")
            || name.contains("Floor")
            || name.contains("Trunc")
            || name.contains("Nearest")
            || name.contains("ExtendLow")
            || name.contains("ExtendHigh")
            || name.contains("ExtAddPairwise")
            || name.contains("Convert")
            || name.contains("Demote")
            || name.contains("Promote")
            || name.contains("Popcnt")
        {
            let _ = state.pop()?;
            state.push(ValType::V128);
        } else if name.starts_with("V128AnyTrue")
            || name.contains("AllTrue")
            || name.contains("Bitmask")
            || name.starts_with("I8x16ExtractLane")
            || name.starts_with("I16x8ExtractLane")
            || name.starts_with("I32x4ExtractLane")
        {
            let _ = state.pop()?;
            state.push(ValType::I32);
        } else if name.starts_with("I64x2ExtractLane") {
            let _ = state.pop()?;
            state.push(ValType::I64);
        } else if name.starts_with("F32x4ExtractLane") {
            let _ = state.pop()?;
            state.push(ValType::F32);
        } else if name.starts_with("F64x2ExtractLane") {
            let _ = state.pop()?;
            state.push(ValType::F64);
        } else if name.contains("ReplaceLane")
            || name.contains("Shl")
            || name.contains("Shr")
            || name.starts_with("V128")
            || name.starts_with("I8x16")
            || name.starts_with("I16x8")
            || name.starts_with("I32x4")
            || name.starts_with("I64x2")
            || name.starts_with("F32x4")
            || name.starts_with("F64x2")
        {
            state.pop_n(2)?;
            state.push(ValType::V128);
        } else {
            return Err(PostlinkError::UnsupportedForkConstruct(format!(
                "unhandled operator in fork-reachable function: {name}"
            )));
        }
        Ok(())
    }
}

struct PassThroughReencoder;

impl Reencode for PassThroughReencoder {
    type Error = String;
}

fn enc_valtype(ty: ValType) -> Result<EncValType, PostlinkError> {
    PassThroughReencoder
        .val_type(ty)
        .map_err(|e| PostlinkError::Reencode(format!("{e:?}")))
}

fn enc_blocktype(ty: BlockType) -> Result<EncBlockType, PostlinkError> {
    PassThroughReencoder
        .block_type(ty)
        .map_err(|e| PostlinkError::Reencode(format!("{e:?}")))
}

fn memarg_64(offset: u64, align: u32) -> MemArg {
    MemArg {
        offset,
        align,
        target: MemArgTarget::Memory(0),
        ordering: None,
    }
}

type SavedEntry = (u32, ValType, u64);

struct FuncEmitter<'m> {
    func_results: Vec<ValType>,
    kb_mode: KbMode,
    next_local: u32,
    added_locals: Vec<EncValType>,
    saved_locals: Vec<(u32, ValType)>,
    rewind_site_local: u32,
    unwind_site_local: u32,
    kb_addr_local: u32,
    frame_ptr_local: u32,
    /// Stack of scopes: `true` = real original label, `false` = synthetic wrapper.
    label_stack: Vec<bool>,
    marker: std::marker::PhantomData<&'m ()>,
}

impl<'m> FuncEmitter<'m> {
    fn new(
        orig_locals: &[ValType],
        func_results: Vec<ValType>,
        kb_mode: KbMode,
    ) -> Result<Self, PostlinkError> {
        let next_local =
            u32::try_from(orig_locals.len()).map_err(|_| PostlinkError::InvalidIndex("locals"))?;
        let mut saved_locals = Vec::new();
        for (i, &ty) in orig_locals.iter().enumerate() {
            if is_savable_scalar(ty) {
                let idx =
                    u32::try_from(i).map_err(|_| PostlinkError::InvalidIndex("orig local"))?;
                saved_locals.push((idx, ty));
            }
        }
        let mut emitter = Self {
            func_results,
            kb_mode,
            next_local,
            added_locals: Vec::new(),
            saved_locals,
            rewind_site_local: 0,
            unwind_site_local: 0,
            kb_addr_local: 0,
            frame_ptr_local: 0,
            label_stack: vec![true], // function body is outermost real label
            marker: std::marker::PhantomData,
        };
        emitter.rewind_site_local = emitter.alloc_temp_local(ValType::I32)?;
        emitter.unwind_site_local = emitter.alloc_temp_local(ValType::I32)?;
        emitter.kb_addr_local = emitter.alloc_temp_local(ValType::I64)?;
        emitter.frame_ptr_local = emitter.alloc_temp_local(ValType::I64)?;
        Ok(emitter)
    }

    fn alloc_temp_local(&mut self, ty: ValType) -> Result<u32, PostlinkError> {
        let idx = self.next_local;
        self.next_local = self
            .next_local
            .checked_add(1)
            .ok_or(PostlinkError::InvalidIndex("temp local"))?;
        self.added_locals.push(enc_valtype(ty)?);
        Ok(idx)
    }

    fn alloc_bridge_locals(
        &mut self,
        tys: &[ValType],
        must_survive_fork: bool,
    ) -> Result<Vec<u32>, PostlinkError> {
        let mut out = Vec::with_capacity(tys.len());
        for &ty in tys {
            if is_savable_scalar(ty) {
                let idx = self.alloc_temp_local(ty)?;
                self.saved_locals.push((idx, ty));
                out.push(idx);
            } else if !must_survive_fork {
                let idx = self.alloc_temp_local(ty)?;
                out.push(idx);
            } else {
                return Err(PostlinkError::UnsupportedForkConstruct(
                    "non-scalar reference value live on operand stack across fork call".into(),
                ));
            }
        }
        Ok(out)
    }

    fn alloc_temp_locals(&mut self, tys: &[ValType]) -> Result<Vec<u32>, PostlinkError> {
        let mut out = Vec::with_capacity(tys.len());
        for &ty in tys {
            out.push(self.alloc_temp_local(ty)?);
        }
        Ok(out)
    }

    fn remap_depth(&self, orig_depth: u32) -> Result<u32, PostlinkError> {
        let mut seen_real = 0_u32;
        let len = self.label_stack.len();
        for (rev_pos, &is_real) in self.label_stack.iter().rev().enumerate() {
            if is_real {
                if seen_real == orig_depth {
                    return u32::try_from(rev_pos)
                        .map_err(|_| PostlinkError::InvalidIndex("branch depth"));
                }
                seen_real = seen_real
                    .checked_add(1)
                    .ok_or(PostlinkError::InvalidIndex("seen_real"))?;
            }
        }
        let _ = len;
        Err(PostlinkError::InvalidIndex("unmapped branch depth"))
    }

    fn frame_layout(&self) -> Result<(u64, Vec<SavedEntry>), PostlinkError> {
        let mut offset = 8_u64; // slot 0..8 holds call_site_id (u64)
        let mut entries = Vec::with_capacity(self.saved_locals.len());
        for &(local_idx, ty) in &self.saved_locals {
            let sz = scalar_slot_bytes(ty)?;
            if sz == 16 && (offset & 15) != 0 {
                offset = offset
                    .checked_add(8)
                    .ok_or(PostlinkError::InvalidIndex("frame align"))?;
            }
            entries.push((local_idx, ty, offset));
            offset = offset
                .checked_add(sz)
                .ok_or(PostlinkError::InvalidIndex("frame size"))?;
        }
        let rem = offset & 15;
        if rem != 0 {
            let pad = 16_u64.saturating_sub(rem);
            offset = offset
                .checked_add(pad)
                .ok_or(PostlinkError::InvalidIndex("frame pad"))?;
        }
        Ok((offset, entries))
    }

    fn emit_compute_kb_addr(&self, out: &mut Vec<Instruction<'m>>) {
        match self.kb_mode {
            KbMode::StaticTls {
                tls_base_global,
                kb_offset_global,
            } => {
                out.push(Instruction::GlobalGet(tls_base_global));
                out.push(Instruction::GlobalGet(kb_offset_global));
                out.push(Instruction::I64Add);
            }
            KbMode::ImportedGlobal(kb_global) => {
                out.push(Instruction::GlobalGet(kb_global));
            }
        }
        out.push(Instruction::LocalTee(self.kb_addr_local));
    }

    fn emit_dummy_value(ty: ValType, out: &mut Vec<Instruction<'m>>) -> Result<(), PostlinkError> {
        match ty {
            ValType::I32 => out.push(Instruction::I32Const(0)),
            ValType::I64 => out.push(Instruction::I64Const(0)),
            ValType::F32 => out.push(Instruction::F32Const(0.0.into())),
            ValType::F64 => out.push(Instruction::F64Const(0.0.into())),
            ValType::V128 => out.push(Instruction::V128Const(0)),
            ValType::Ref(rty) => {
                if !rty.is_nullable() {
                    return Err(PostlinkError::UnsupportedForkConstruct(
                        "non-nullable reference return in fork-reachable function".into(),
                    ));
                }
                let enc_hty = PassThroughReencoder
                    .heap_type(rty.heap_type())
                    .map_err(|e| PostlinkError::Reencode(format!("{e:?}")))?;
                out.push(Instruction::RefNull(enc_hty));
            }
        }
        Ok(())
    }

    /// Per-callsite O(1) unwind check: records `site_id` in `unwind_site_local` and branches
    /// to the shared per-function `$unwind_exit` epilogue block (`O(S + L)` instead of `O(S * L)`).
    fn emit_unwind_branch(
        &self,
        site_id: u32,
        out: &mut Vec<Instruction<'m>>,
    ) -> Result<(), PostlinkError> {
        // if (i32.load(kb + STATE_OFFSET) == STATE_UNWINDING)
        self.emit_compute_kb_addr(out);
        out.push(Instruction::I32Load(memarg_64(kernel_block::STATE_OFFSET, 2)));
        out.push(Instruction::I32Const(
            i32::try_from(kernel_block::STATE_UNWINDING).unwrap_or(1),
        ));
        out.push(Instruction::I32Eq);
        out.push(Instruction::If(EncBlockType::Empty));
        out.push(Instruction::I32Const(
            i32::try_from(site_id).unwrap_or(i32::MAX),
        ));
        out.push(Instruction::LocalSet(self.unwind_site_local));
        // `self.label_stack[1]` is `$unwind_exit`; accounting for this `If` itself (+1),
        // its relative branch depth is `(self.label_stack.len() + 1) - 2 = self.label_stack.len() - 1`.
        let unwind_depth = u32::try_from(self.label_stack.len().saturating_sub(1))
            .map_err(|_| PostlinkError::InvalidIndex("unwind_depth"))?;
        out.push(Instruction::Br(unwind_depth));
        out.push(Instruction::End);
        Ok(())
    }

    /// Emitted once per instrumented function after `end $unwind_exit`.
    fn emit_unwind_epilogue(
        &self,
        frame_bytes: u64,
        entries: &[(u32, ValType, u64)],
        out: &mut Vec<Instruction<'m>>,
    ) -> Result<(), PostlinkError> {
        self.emit_compute_kb_addr(out);
        out.push(Instruction::I64Load(memarg_64(
            kernel_block::FRAME_BUF_TOP_OFFSET,
            3,
        )));
        out.push(Instruction::LocalTee(self.frame_ptr_local));

        // new_top = frame_ptr + frame_bytes; trap if new_top > FRAME_BUF_END
        let frame_bytes_i64 =
            i64::try_from(frame_bytes).map_err(|_| PostlinkError::InvalidIndex("frame_bytes"))?;
        out.push(Instruction::I64Const(frame_bytes_i64));
        out.push(Instruction::I64Add);
        out.push(Instruction::LocalGet(self.kb_addr_local));
        out.push(Instruction::I64Load(memarg_64(
            kernel_block::FRAME_BUF_END_OFFSET,
            3,
        )));
        out.push(Instruction::I64GtU);
        out.push(Instruction::If(EncBlockType::Empty));
        out.push(Instruction::Unreachable);
        out.push(Instruction::End);

        // Advance FRAME_BUF_TOP = frame_ptr + frame_bytes
        out.push(Instruction::LocalGet(self.kb_addr_local));
        out.push(Instruction::LocalGet(self.frame_ptr_local));
        out.push(Instruction::I64Const(frame_bytes_i64));
        out.push(Instruction::I64Add);
        out.push(Instruction::I64Store(memarg_64(
            kernel_block::FRAME_BUF_TOP_OFFSET,
            3,
        )));

        // Store i64.extend_i32_u(unwind_site_local) at frame_ptr + 0
        out.push(Instruction::LocalGet(self.frame_ptr_local));
        out.push(Instruction::LocalGet(self.unwind_site_local));
        out.push(Instruction::I64ExtendI32U);
        out.push(Instruction::I64Store(memarg_64(0, 3)));

        // Store all saved scalar locals once
        for &(local_idx, ty, off) in entries {
            out.push(Instruction::LocalGet(self.frame_ptr_local));
            out.push(Instruction::LocalGet(local_idx));
            match ty {
                ValType::I32 => out.push(Instruction::I32Store(memarg_64(off, 2))),
                ValType::I64 => out.push(Instruction::I64Store(memarg_64(off, 3))),
                ValType::F32 => out.push(Instruction::F32Store(memarg_64(off, 2))),
                ValType::F64 => out.push(Instruction::F64Store(memarg_64(off, 3))),
                ValType::V128 => out.push(Instruction::V128Store(memarg_64(off, 4))),
                ValType::Ref(_) => {}
            }
        }

        for &rty in &self.func_results {
            Self::emit_dummy_value(rty, out)?;
        }
        Ok(())
    }

    fn emit_rewind_prologue(
        &self,
        frame_bytes: u64,
        entries: &[(u32, ValType, u64)],
        out: &mut Vec<Instruction<'m>>,
    ) -> Result<(), PostlinkError> {
        let frame_bytes_i64 =
            i64::try_from(frame_bytes).map_err(|_| PostlinkError::InvalidIndex("frame_bytes"))?;

        // if (i32.load(kb + STATE_OFFSET) == STATE_REWINDING)
        self.emit_compute_kb_addr(out);
        out.push(Instruction::I32Load(memarg_64(kernel_block::STATE_OFFSET, 2)));
        out.push(Instruction::I32Const(
            i32::try_from(kernel_block::STATE_REWINDING).unwrap_or(2),
        ));
        out.push(Instruction::I32Eq);
        out.push(Instruction::If(EncBlockType::Empty));

        // frame_ptr = i64.load(kb + FRAME_BUF_TOP_OFFSET) - frame_bytes
        out.push(Instruction::LocalGet(self.kb_addr_local));
        out.push(Instruction::I64Load(memarg_64(
            kernel_block::FRAME_BUF_TOP_OFFSET,
            3,
        )));
        out.push(Instruction::I64Const(frame_bytes_i64));
        out.push(Instruction::I64Sub);
        out.push(Instruction::LocalTee(self.frame_ptr_local));

        // Bounds check: frame_ptr < FRAME_BUF_START => unreachable
        out.push(Instruction::LocalGet(self.kb_addr_local));
        out.push(Instruction::I64Load(memarg_64(
            kernel_block::FRAME_BUF_START_OFFSET,
            3,
        )));
        out.push(Instruction::I64LtU);
        out.push(Instruction::If(EncBlockType::Empty));
        out.push(Instruction::Unreachable);
        out.push(Instruction::End);

        // Write updated FRAME_BUF_TOP = frame_ptr
        out.push(Instruction::LocalGet(self.kb_addr_local));
        out.push(Instruction::LocalGet(self.frame_ptr_local));
        out.push(Instruction::I64Store(memarg_64(
            kernel_block::FRAME_BUF_TOP_OFFSET,
            3,
        )));

        // Load rewind_site_local = i32.wrap_i64(i64.load(frame_ptr + 0))
        out.push(Instruction::LocalGet(self.frame_ptr_local));
        out.push(Instruction::I64Load(memarg_64(0, 3)));
        out.push(Instruction::I32WrapI64);
        out.push(Instruction::LocalSet(self.rewind_site_local));

        // Restore all saved scalar locals
        for &(local_idx, ty, off) in entries {
            out.push(Instruction::LocalGet(self.frame_ptr_local));
            match ty {
                ValType::I32 => out.push(Instruction::I32Load(memarg_64(off, 2))),
                ValType::I64 => out.push(Instruction::I64Load(memarg_64(off, 3))),
                ValType::F32 => out.push(Instruction::F32Load(memarg_64(off, 2))),
                ValType::F64 => out.push(Instruction::F64Load(memarg_64(off, 3))),
                ValType::V128 => out.push(Instruction::V128Load(memarg_64(off, 4))),
                ValType::Ref(_) => {}
            }
            out.push(Instruction::LocalSet(local_idx));
        }

        out.push(Instruction::End);
        Ok(())
    }

    fn emit_plain_nodes(
        &mut self,
        nodes: &[AstNode<'m>],
        out: &mut Vec<Instruction<'m>>,
    ) -> Result<(), PostlinkError> {
        for node in nodes {
            match node {
                AstNode::Instr { op, .. } => self.emit_plain_op(op, out)?,
                AstNode::Block { blockty, body, .. } => {
                    out.push(Instruction::Block(enc_blocktype(*blockty)?));
                    self.label_stack.push(true);
                    self.emit_plain_nodes(body, out)?;
                    let _ = self.label_stack.pop();
                    out.push(Instruction::End);
                }
                AstNode::Loop { blockty, body, .. } => {
                    out.push(Instruction::Loop(enc_blocktype(*blockty)?));
                    self.label_stack.push(true);
                    self.emit_plain_nodes(body, out)?;
                    let _ = self.label_stack.pop();
                    out.push(Instruction::End);
                }
                AstNode::If {
                    blockty,
                    then_body,
                    else_body,
                    has_else,
                    ..
                } => {
                    out.push(Instruction::If(enc_blocktype(*blockty)?));
                    self.label_stack.push(true);
                    self.emit_plain_nodes(then_body, out)?;
                    if *has_else {
                        out.push(Instruction::Else);
                        self.emit_plain_nodes(else_body, out)?;
                    }
                    let _ = self.label_stack.pop();
                    out.push(Instruction::End);
                }
                AstNode::TryTable {
                    blockty,
                    catches,
                    body,
                    ..
                } => {
                    let enc_catches = self.remap_catches(catches)?;
                    out.push(Instruction::TryTable(
                        enc_blocktype(*blockty)?,
                        enc_catches.into(),
                    ));
                    self.label_stack.push(true);
                    self.emit_plain_nodes(body, out)?;
                    let _ = self.label_stack.pop();
                    out.push(Instruction::End);
                }
            }
        }
        Ok(())
    }

    fn remap_catches(&self, catches: &[Catch]) -> Result<Vec<EncCatch>, PostlinkError> {
        let mut out = Vec::with_capacity(catches.len());
        for c in catches {
            let mapped = match *c {
                Catch::One { tag, label } => EncCatch::One {
                    tag,
                    label: self.remap_depth(label)?,
                },
                Catch::OneRef { tag, label } => EncCatch::OneRef {
                    tag,
                    label: self.remap_depth(label)?,
                },
                Catch::All { label } => EncCatch::All {
                    label: self.remap_depth(label)?,
                },
                Catch::AllRef { label } => EncCatch::AllRef {
                    label: self.remap_depth(label)?,
                },
            };
            out.push(mapped);
        }
        Ok(out)
    }

    fn emit_plain_op(
        &self,
        op: &Operator<'m>,
        out: &mut Vec<Instruction<'m>>,
    ) -> Result<(), PostlinkError> {
        match op {
            Operator::Br { relative_depth } => {
                out.push(Instruction::Br(self.remap_depth(*relative_depth)?));
            }
            Operator::BrIf { relative_depth } => {
                out.push(Instruction::BrIf(self.remap_depth(*relative_depth)?));
            }
            Operator::BrTable { targets } => {
                let mut mapped = Vec::with_capacity(targets.len() as usize);
                for t in targets.targets() {
                    mapped.push(self.remap_depth(t?)?);
                }
                let default_t = self.remap_depth(targets.default())?;
                out.push(Instruction::BrTable(mapped.into(), default_t));
            }
            other => {
                let inst = reencode::utils::instruction(&mut PassThroughReencoder, other.clone())
                    .map_err(|e| PostlinkError::Reencode(format!("{e:?}")))?;
                out.push(inst);
            }
        }
        Ok(())
    }

    fn emit_range_check(&self, lo: u32, hi: u32, out: &mut Vec<Instruction<'m>>) {
        let lo_i32 = i32::try_from(lo).unwrap_or(i32::MAX);
        let hi_i32 = i32::try_from(hi).unwrap_or(i32::MAX);
        if lo == hi {
            out.push(Instruction::LocalGet(self.rewind_site_local));
            out.push(Instruction::I32Const(lo_i32));
            out.push(Instruction::I32Eq);
        } else {
            out.push(Instruction::LocalGet(self.rewind_site_local));
            out.push(Instruction::I32Const(lo_i32));
            out.push(Instruction::I32GeU);
            out.push(Instruction::LocalGet(self.rewind_site_local));
            out.push(Instruction::I32Const(hi_i32));
            out.push(Instruction::I32LeU);
            out.push(Instruction::I32And);
        }
    }

    /// Two-pass emission: first pass allocates all stack-spill locals so `saved_locals`
    /// is finalized before computing `frame_layout()`; second pass emits final bytes.
    fn build_function(
        &mut self,
        nodes: &[AstNode<'m>],
        end_polymorphic: bool,
    ) -> Result<Function, PostlinkError> {
        let mut plan = self.plan_scope(nodes, &[], end_polymorphic)?;
        let (frame_bytes, entries) = self.frame_layout()?;

        let mut instrs: Vec<Instruction<'m>> = Vec::new();
        instrs.push(Instruction::Block(EncBlockType::Empty));
        self.label_stack.push(false); // index 1 = $unwind_exit
        self.emit_rewind_prologue(frame_bytes, &entries, &mut instrs)?;
        self.emit_planned_scope(&mut plan, &mut instrs)?;
        let _ = self.label_stack.pop();
        instrs.push(Instruction::Return);
        instrs.push(Instruction::End); // end $unwind_exit
        self.emit_unwind_epilogue(frame_bytes, &entries, &mut instrs)?;
        instrs.push(Instruction::End); // end function

        let local_decls: Vec<(u32, EncValType)> =
            self.added_locals.iter().map(|&ty| (1, ty)).collect();
        let mut func = Function::new(local_decls);
        for inst in &instrs {
            func.instruction(inst);
        }
        Ok(func)
    }

    fn plan_scope(
        &mut self,
        nodes: &[AstNode<'m>],
        scope_params: &[ValType],
        scope_end_polymorphic: bool,
    ) -> Result<PlannedScope<'m>, PostlinkError> {
        let param_locals = self.alloc_bridge_locals(scope_params, false)?;
        let mut segments = Vec::new();
        let mut cur_prefix = Vec::new();
        let mut cur_in_locals = param_locals.clone();

        for node in nodes {
            if node.fork_range().is_some() {
                let before_tys = node.in_stack();
                let before_locals = self.alloc_bridge_locals(before_tys, true)?;
                let prefix_ends_poly = prefix_is_polymorphic(&cur_prefix);
                segments.push(PlannedStep::Prefix {
                    nodes: std::mem::take(&mut cur_prefix),
                    in_locals: cur_in_locals,
                    out_locals: before_locals.clone(),
                    ends_polymorphic: prefix_ends_poly,
                });

                let after_tys = node.out_stack();
                let (fork_step, after_locals) =
                    self.plan_fork_node(node, &before_locals, after_tys)?;
                segments.push(fork_step);
                cur_in_locals = after_locals;
            } else {
                cur_prefix.push(node.clone());
            }
        }

        let _ = scope_end_polymorphic;
        Ok(PlannedScope {
            param_locals,
            steps: segments,
            suffix: cur_prefix,
            suffix_in_locals: cur_in_locals,
        })
    }

    #[allow(clippy::too_many_lines)]
    fn plan_fork_node(
        &mut self,
        node: &AstNode<'m>,
        before_locals: &[u32],
        after_tys: &[ValType],
    ) -> Result<(PlannedStep<'m>, Vec<u32>), PostlinkError> {
        match node {
            AstNode::Instr {
                op,
                fork_site: Some(site_id),
                call_params,
                call_results,
                is_indirect,
                ..
            } => {
                let extra_idx = usize::from(*is_indirect);
                let consumed = call_params
                    .len()
                    .checked_add(extra_idx)
                    .ok_or(PostlinkError::InvalidIndex("call consumed"))?;
                let below_len = before_locals
                    .len()
                    .checked_sub(consumed)
                    .ok_or(PostlinkError::InvalidIndex("call below_len"))?;
                let below_locals = before_locals
                    .get(..below_len)
                    .ok_or(PostlinkError::InvalidIndex("below slice"))?
                    .to_vec();
                let arg_locals = before_locals
                    .get(below_len..)
                    .ok_or(PostlinkError::InvalidIndex("arg slice"))?
                    .to_vec();
                let ret_saved = self.alloc_bridge_locals(call_results, false)?;
                let mut after_locals = below_locals;
                after_locals.extend_from_slice(&ret_saved);
                debug_assert_eq!(after_locals.len(), after_tys.len());
                Ok((
                    PlannedStep::ForkCall {
                        op: op.clone(),
                        site_id: *site_id,
                        arg_locals,
                        ret_locals: ret_saved,
                    },
                    after_locals,
                ))
            }
            AstNode::Block {
                blockty,
                body,
                fork_range: Some((lo, hi)),
                params,
                results,
                body_end_polymorphic,
                ..
            } => {
                let below_len = before_locals
                    .len()
                    .checked_sub(params.len())
                    .ok_or(PostlinkError::InvalidIndex("block below"))?;
                let below_locals = before_locals
                    .get(..below_len)
                    .ok_or(PostlinkError::InvalidIndex("block below slice"))?
                    .to_vec();
                let block_in_locals = before_locals
                    .get(below_len..)
                    .ok_or(PostlinkError::InvalidIndex("block in slice"))?
                    .to_vec();
                let inner = self.plan_scope(body, params, *body_end_polymorphic)?;
                let res_locals = self.alloc_bridge_locals(results, false)?;
                let mut after_locals = below_locals;
                after_locals.extend_from_slice(&res_locals);
                Ok((
                    PlannedStep::ForkBlock {
                        kind: CompositeKind::Block,
                        blockty: *blockty,
                        catches: Vec::new(),
                        lo: *lo,
                        hi: *hi,
                        params: params.clone(),
                        block_in_locals,
                        res_locals,
                        inner,
                    },
                    after_locals,
                ))
            }
            AstNode::Loop {
                blockty,
                body,
                fork_range: Some((lo, hi)),
                params,
                results,
                body_end_polymorphic,
                ..
            } => {
                let below_len = before_locals
                    .len()
                    .checked_sub(params.len())
                    .ok_or(PostlinkError::InvalidIndex("loop below"))?;
                let below_locals = before_locals
                    .get(..below_len)
                    .ok_or(PostlinkError::InvalidIndex("loop below slice"))?
                    .to_vec();
                let block_in_locals = before_locals
                    .get(below_len..)
                    .ok_or(PostlinkError::InvalidIndex("loop in slice"))?
                    .to_vec();
                let inner = self.plan_scope(body, params, *body_end_polymorphic)?;
                let res_locals = self.alloc_bridge_locals(results, false)?;
                let mut after_locals = below_locals;
                after_locals.extend_from_slice(&res_locals);
                Ok((
                    PlannedStep::ForkBlock {
                        kind: CompositeKind::Loop,
                        blockty: *blockty,
                        catches: Vec::new(),
                        lo: *lo,
                        hi: *hi,
                        params: params.clone(),
                        block_in_locals,
                        res_locals,
                        inner,
                    },
                    after_locals,
                ))
            }
            AstNode::TryTable {
                blockty,
                catches,
                body,
                fork_range: Some((lo, hi)),
                params,
                results,
                body_end_polymorphic,
                ..
            } => {
                let below_len = before_locals
                    .len()
                    .checked_sub(params.len())
                    .ok_or(PostlinkError::InvalidIndex("try_table below"))?;
                let below_locals = before_locals
                    .get(..below_len)
                    .ok_or(PostlinkError::InvalidIndex("try_table below slice"))?
                    .to_vec();
                let block_in_locals = before_locals
                    .get(below_len..)
                    .ok_or(PostlinkError::InvalidIndex("try_table in slice"))?
                    .to_vec();
                let inner = self.plan_scope(body, params, *body_end_polymorphic)?;
                let res_locals = self.alloc_bridge_locals(results, false)?;
                let mut after_locals = below_locals;
                after_locals.extend_from_slice(&res_locals);
                Ok((
                    PlannedStep::ForkBlock {
                        kind: CompositeKind::TryTable,
                        blockty: *blockty,
                        catches: catches.clone(),
                        lo: *lo,
                        hi: *hi,
                        params: params.clone(),
                        block_in_locals,
                        res_locals,
                        inner,
                    },
                    after_locals,
                ))
            }
            AstNode::If {
                blockty,
                then_body,
                else_body,
                has_else,
                then_range,
                else_range,
                fork_range: Some((lo, hi)),
                params,
                results,
                then_end_polymorphic,
                else_end_polymorphic,
                ..
            } => {
                let consumed = params
                    .len()
                    .checked_add(1)
                    .ok_or(PostlinkError::InvalidIndex("if consumed"))?;
                let below_len = before_locals
                    .len()
                    .checked_sub(consumed)
                    .ok_or(PostlinkError::InvalidIndex("if below"))?;
                let below_locals = before_locals
                    .get(..below_len)
                    .ok_or(PostlinkError::InvalidIndex("if below slice"))?
                    .to_vec();
                let cond_idx = before_locals
                    .len()
                    .checked_sub(1)
                    .ok_or(PostlinkError::InvalidIndex("if cond"))?;
                let cond_local = *before_locals
                    .get(cond_idx)
                    .ok_or(PostlinkError::InvalidIndex("if cond local"))?;
                let block_in_locals = before_locals
                    .get(below_len..cond_idx)
                    .ok_or(PostlinkError::InvalidIndex("if params slice"))?
                    .to_vec();

                let then_plan = if then_range.is_some() {
                    IfBranchPlan::Fork(self.plan_scope(then_body, params, *then_end_polymorphic)?)
                } else {
                    IfBranchPlan::Plain(then_body.clone())
                };
                let else_plan = if else_range.is_some() {
                    IfBranchPlan::Fork(self.plan_scope(else_body, params, *else_end_polymorphic)?)
                } else {
                    IfBranchPlan::Plain(else_body.clone())
                };

                let res_locals = self.alloc_bridge_locals(results, false)?;
                let mut after_locals = below_locals;
                after_locals.extend_from_slice(&res_locals);
                let _ = self.alloc_temp_locals(&[]);
                Ok((
                    PlannedStep::ForkIf {
                        blockty: *blockty,
                        has_else: *has_else,
                        lo: *lo,
                        hi: *hi,
                        then_range: *then_range,
                        params: params.clone(),
                        block_in_locals,
                        cond_local,
                        res_locals,
                        then_plan,
                        else_plan,
                    },
                    after_locals,
                ))
            }
            _ => Err(PostlinkError::UnsupportedForkConstruct(
                "unexpected non-fork node in plan_fork_node".into(),
            )),
        }
    }

    #[allow(clippy::too_many_lines)]
    fn emit_planned_scope(
        &mut self,
        plan: &mut PlannedScope<'m>,
        out: &mut Vec<Instruction<'m>>,
    ) -> Result<(), PostlinkError> {
        if !plan.param_locals.is_empty() {
            // Drain incoming block params: store into param_locals if running normally, else drop.
            out.push(Instruction::LocalGet(self.rewind_site_local));
            out.push(Instruction::I32Eqz);
            out.push(Instruction::If(EncBlockType::Empty));
            for &loc in plan.param_locals.iter().rev() {
                out.push(Instruction::LocalSet(loc));
            }
            out.push(Instruction::Else);
            for _ in &plan.param_locals {
                out.push(Instruction::Drop);
            }
            out.push(Instruction::End);
        }

        for step in &mut plan.steps {
            match step {
                PlannedStep::Prefix {
                    nodes,
                    in_locals,
                    out_locals,
                    ends_polymorphic,
                } => {
                    if nodes.is_empty() && in_locals == out_locals {
                        continue;
                    }
                    out.push(Instruction::LocalGet(self.rewind_site_local));
                    out.push(Instruction::I32Eqz);
                    out.push(Instruction::If(EncBlockType::Empty));
                    self.label_stack.push(false);
                    for &loc in in_locals.iter() {
                        out.push(Instruction::LocalGet(loc));
                    }
                    self.emit_plain_nodes(nodes, out)?;
                    if !*ends_polymorphic {
                        for &loc in out_locals.iter().rev() {
                            out.push(Instruction::LocalSet(loc));
                        }
                    }
                    let _ = self.label_stack.pop();
                    out.push(Instruction::End);
                }
                PlannedStep::ForkCall {
                    op,
                    site_id,
                    arg_locals,
                    ret_locals,
                } => {
                    // if ($rewind_site == 0 || $rewind_site == site_id)
                    out.push(Instruction::LocalGet(self.rewind_site_local));
                    out.push(Instruction::I32Eqz);
                    self.emit_range_check(*site_id, *site_id, out);
                    out.push(Instruction::I32Or);
                    out.push(Instruction::If(EncBlockType::Empty));
                    self.label_stack.push(false);

                    // Clear $rewind_site = 0 before entering callee so post-call runs normally.
                    out.push(Instruction::I32Const(0));
                    out.push(Instruction::LocalSet(self.rewind_site_local));

                    for &loc in arg_locals.iter() {
                        out.push(Instruction::LocalGet(loc));
                    }
                    self.emit_plain_op(op, out)?;
                    for &loc in ret_locals.iter().rev() {
                        out.push(Instruction::LocalSet(loc));
                    }

                    self.emit_unwind_branch(*site_id, out)?;

                    let _ = self.label_stack.pop();
                    out.push(Instruction::End);
                }
                PlannedStep::ForkBlock {
                    kind,
                    blockty,
                    catches,
                    lo,
                    hi,
                    params,
                    block_in_locals,
                    res_locals,
                    inner,
                } => {
                    out.push(Instruction::LocalGet(self.rewind_site_local));
                    out.push(Instruction::I32Eqz);
                    self.emit_range_check(*lo, *hi, out);
                    out.push(Instruction::I32Or);
                    out.push(Instruction::If(EncBlockType::Empty));
                    self.label_stack.push(false);

                    self.emit_push_block_params(params, block_in_locals, out)?;

                    match kind {
                        CompositeKind::Block => {
                            out.push(Instruction::Block(enc_blocktype(*blockty)?));
                        }
                        CompositeKind::Loop => {
                            out.push(Instruction::Loop(enc_blocktype(*blockty)?));
                        }
                        CompositeKind::TryTable => {
                            let enc_catches = self.remap_catches(catches)?;
                            out.push(Instruction::TryTable(
                                enc_blocktype(*blockty)?,
                                enc_catches.into(),
                            ));
                        }
                    }
                    self.label_stack.push(true);
                    self.emit_planned_scope(inner, out)?;
                    let _ = self.label_stack.pop();
                    out.push(Instruction::End);

                    for &loc in res_locals.iter().rev() {
                        out.push(Instruction::LocalSet(loc));
                    }

                    let _ = self.label_stack.pop();
                    out.push(Instruction::End);
                }
                PlannedStep::ForkIf {
                    blockty,
                    has_else,
                    lo,
                    hi,
                    then_range,
                    params,
                    block_in_locals,
                    cond_local,
                    res_locals,
                    then_plan,
                    else_plan,
                } => {
                    out.push(Instruction::LocalGet(self.rewind_site_local));
                    out.push(Instruction::I32Eqz);
                    self.emit_range_check(*lo, *hi, out);
                    out.push(Instruction::I32Or);
                    out.push(Instruction::If(EncBlockType::Empty));
                    self.label_stack.push(false);

                    self.emit_push_block_params(params, block_in_locals, out)?;

                    // Select `if` condition: normal -> cond_local; rewinding -> whether target is in `then`.
                    out.push(Instruction::LocalGet(self.rewind_site_local));
                    out.push(Instruction::I32Eqz);
                    out.push(Instruction::If(EncBlockType::Result(EncValType::I32)));
                    out.push(Instruction::LocalGet(*cond_local));
                    out.push(Instruction::Else);
                    if let Some((t_lo, t_hi)) = *then_range {
                        self.emit_range_check(t_lo, t_hi, out);
                    } else {
                        out.push(Instruction::I32Const(0));
                    }
                    out.push(Instruction::End);

                    out.push(Instruction::If(enc_blocktype(*blockty)?));
                    self.label_stack.push(true);
                    match then_plan {
                        IfBranchPlan::Fork(inner) => {
                            self.emit_planned_scope(inner, out)?;
                        }
                        IfBranchPlan::Plain(plain_nodes) => {
                            self.emit_plain_nodes(plain_nodes, out)?;
                        }
                    }
                    if *has_else {
                        out.push(Instruction::Else);
                        match else_plan {
                            IfBranchPlan::Fork(inner) => {
                                self.emit_planned_scope(inner, out)?;
                            }
                            IfBranchPlan::Plain(plain_nodes) => {
                                self.emit_plain_nodes(plain_nodes, out)?;
                            }
                        }
                    }
                    let _ = self.label_stack.pop();
                    out.push(Instruction::End);

                    for &loc in res_locals.iter().rev() {
                        out.push(Instruction::LocalSet(loc));
                    }

                    let _ = self.label_stack.pop();
                    out.push(Instruction::End);
                }
            }
        }

        // Suffix runs unconditionally with $rewind_site == 0!
        for &loc in &plan.suffix_in_locals {
            out.push(Instruction::LocalGet(loc));
        }
        self.emit_plain_nodes(&plan.suffix, out)?;
        Ok(())
    }

    fn emit_push_block_params(
        &self,
        params: &[ValType],
        block_in_locals: &[u32],
        out: &mut Vec<Instruction<'m>>,
    ) -> Result<(), PostlinkError> {
        if params.is_empty() {
            return Ok(());
        }
        for (&ty, &loc) in params.iter().zip(block_in_locals.iter()) {
            out.push(Instruction::LocalGet(self.rewind_site_local));
            out.push(Instruction::I32Eqz);
            out.push(Instruction::If(EncBlockType::Result(enc_valtype(ty)?)));
            out.push(Instruction::LocalGet(loc));
            out.push(Instruction::Else);
            Self::emit_dummy_value(ty, out)?;
            out.push(Instruction::End);
        }
        Ok(())
    }
}

#[derive(Clone, Copy)]
enum CompositeKind {
    Block,
    Loop,
    TryTable,
}

enum IfBranchPlan<'m> {
    Fork(PlannedScope<'m>),
    Plain(Vec<AstNode<'m>>),
}

struct PlannedScope<'m> {
    param_locals: Vec<u32>,
    steps: Vec<PlannedStep<'m>>,
    suffix: Vec<AstNode<'m>>,
    suffix_in_locals: Vec<u32>,
}

enum PlannedStep<'m> {
    Prefix {
        nodes: Vec<AstNode<'m>>,
        in_locals: Vec<u32>,
        out_locals: Vec<u32>,
        ends_polymorphic: bool,
    },
    ForkCall {
        op: Operator<'m>,
        site_id: u32,
        arg_locals: Vec<u32>,
        ret_locals: Vec<u32>,
    },
    ForkBlock {
        kind: CompositeKind,
        blockty: BlockType,
        catches: Vec<Catch>,
        lo: u32,
        hi: u32,
        params: Vec<ValType>,
        block_in_locals: Vec<u32>,
        res_locals: Vec<u32>,
        inner: PlannedScope<'m>,
    },
    ForkIf {
        blockty: BlockType,
        has_else: bool,
        lo: u32,
        hi: u32,
        then_range: Option<(u32, u32)>,
        params: Vec<ValType>,
        block_in_locals: Vec<u32>,
        cond_local: u32,
        res_locals: Vec<u32>,
        then_plan: IfBranchPlan<'m>,
        else_plan: IfBranchPlan<'m>,
    },
}

fn prefix_is_polymorphic(nodes: &[AstNode<'_>]) -> bool {
    let Some(last) = nodes.last() else {
        return false;
    };
    matches!(
        last,
        AstNode::Instr {
            op: Operator::Unreachable
                | Operator::Return
                | Operator::Br { .. }
                | Operator::BrTable { .. }
                | Operator::ReturnCall { .. }
                | Operator::ReturnCallIndirect { .. }
                | Operator::Throw { .. }
                | Operator::ThrowRef,
            ..
        }
    )
}

struct ForkRewriter<'m> {
    info: &'m ModuleInfo,
    fork_funcs: &'m BTreeSet<u32>,
    fork_indirect_sigs: &'m BTreeSet<FuncType>,
    /// Map from imported fork syscall function index -> synthesized trampoline function index.
    trampolines: BTreeMap<u32, u32>,
    trampoline_list: Vec<(u32, u32)>, // (import_func_idx, type_idx)
    kb_mode: KbMode,
}

impl<'m> Reencode for ForkRewriter<'m> {
    type Error = PostlinkError;

    fn parse_function_section(
        &mut self,
        functions: &mut FunctionSection,
        section: FunctionSectionReader<'_>,
    ) -> Result<(), reencode::Error<Self::Error>> {
        reencode::utils::parse_function_section(self, functions, section)?;
        for (_, type_idx) in self.trampoline_list.clone() {
            functions.function(self.type_index(type_idx)?);
        }
        Ok(())
    }

    fn parse_code_section(
        &mut self,
        code: &mut CodeSection,
        section: CodeSectionReader<'_>,
    ) -> Result<(), reencode::Error<Self::Error>> {
        let imp_count = self.info.num_imported_funcs();
        for (idx, body) in section.into_iter().enumerate() {
            let body = body?;
            let def_idx = u32::try_from(idx)
                .map_err(|_| reencode::Error::UserError(PostlinkError::InvalidIndex("code idx")))?;
            let func_idx = imp_count
                .checked_add(def_idx)
                .ok_or(reencode::Error::UserError(PostlinkError::InvalidIndex(
                    "func_idx",
                )))?;

            if !self.fork_funcs.contains(&func_idx) {
                self.parse_function_body(code, body)?;
                continue;
            }

            let rewritten = self
                .rewrite_defined_function(func_idx, body)
                .map_err(reencode::Error::UserError)?;
            code.function(&rewritten);
        }

        for &(import_func_idx, type_idx) in &self.trampoline_list {
            let tramp = self
                .build_fork_trampoline(import_func_idx, type_idx)
                .map_err(reencode::Error::UserError)?;
            code.function(&tramp);
        }
        Ok(())
    }

    fn parse_custom_section(
        &mut self,
        module: &mut Module,
        section: CustomSectionReader<'_>,
    ) -> Result<(), reencode::Error<Self::Error>> {
        if section.name() == "name" {
            // Preserve module name (`--soname=...` for shared libraries) while dropping
            // function/local debug names invalidated by synthetic trampolines.
            let mut names = NameSection::new();
            let mut has_module_name = false;
            let reader = NameSectionReader::new(BinaryReader::new(section.data(), 0));
            for sub in reader {
                if let Ok(Name::Module { name, .. }) = sub {
                    names.module(name);
                    has_module_name = true;
                }
            }
            if has_module_name {
                module.section(&names);
            }
            return Ok(());
        }
        if section.name() == FORK_EXPORTS_SECTION_NAME {
            return Ok(());
        }
        if section.name() == TARGET_SECTION_NAME {
            let mut hdr = self.info.target_header.unwrap_or(TargetHeader {
                major: wasmos_abi::TARGET_ABI_MAJOR,
                minor: 0,
                profile: 0,
                flags: 0,
            });
            hdr.flags |= TargetHeader::FLAG_FORK_INSTRUMENTED;
            let encoded = hdr.encode();
            module.section(&CustomSection {
                name: TARGET_SECTION_NAME.into(),
                data: encoded.as_slice().into(),
            });
            return Ok(());
        }
        reencode::utils::parse_custom_section(self, module, section)
    }

    fn parse_export_section(
        &mut self,
        exports: &mut wasm_encoder::ExportSection,
        section: ExportSectionReader<'_>,
    ) -> Result<(), reencode::Error<Self::Error>> {
        let mut has_table_export = false;
        for export in section {
            let export = export?;
            if export.name == "__indirect_function_table" {
                has_table_export = true;
            }
            let kind = self.export_kind(export.kind)?;
            let index = match export.kind {
                ExternalKind::Func => self.function_index(export.index)?,
                ExternalKind::Table => self.table_index(export.index)?,
                ExternalKind::Memory => self.memory_index(export.index)?,
                ExternalKind::Global => self.global_index(export.index)?,
                ExternalKind::Tag => self.tag_index(export.index)?,
                ExternalKind::FuncExact => self.function_index(export.index)?,
            };
            let _ = ExportKind::Func;
            exports.export(export.name, kind, index);
        }
        if !has_table_export && !self.info.has_imported_table {
            exports.export("__indirect_function_table", ExportKind::Table, 0);
        }
        Ok(())
    }
}

impl<'m> ForkRewriter<'m> {
    fn rewrite_defined_function(
        &self,
        func_idx: u32,
        body: FunctionBody<'_>,
    ) -> Result<Function, PostlinkError> {
        let fty = self.info.func_type(func_idx)?.clone();
        let mut all_locals: Vec<ValType> = fty.params().to_vec();
        let locals_reader = body.get_locals_reader()?;
        for decl in locals_reader {
            let (count, ty) = decl?;
            for _ in 0..count {
                all_locals.push(ty);
            }
        }

        let mut ops = Vec::new();
        let mut ops_reader = body.get_operators_reader()?;
        while !ops_reader.eof() {
            ops.push(ops_reader.read()?);
        }

        let mut tracker = TypeTracker {
            info: self.info,
            locals: all_locals.clone(),
            fork_callee_map: &self.trampolines,
            fork_funcs: self.fork_funcs,
            fork_indirect_sigs: self.fork_indirect_sigs,
            next_site_id: 1,
        };
        let (ast, end_poly) = tracker.parse_body(&ops)?;
        if list_fork_range(&ast).is_none() {
            // Unreachable dead call edge only; emit original body untouched via plain emitter.
            let mut emitter = FuncEmitter::new(&all_locals, fty.results().to_vec(), self.kb_mode)?;
            emitter.added_locals.clear();
            let mut decls = Vec::new();
            for &ty in all_locals.get(fty.params().len()..).unwrap_or(&[]) {
                decls.push((1, enc_valtype(ty)?));
            }
            let mut func = Function::new(decls);
            let mut out = Vec::new();
            emitter.emit_plain_nodes(&ast, &mut out)?;
            out.push(Instruction::End);
            for i in &out {
                func.instruction(i);
            }
            return Ok(func);
        }

        let orig_declared_len = all_locals
            .len()
            .checked_sub(fty.params().len())
            .ok_or(PostlinkError::InvalidIndex("declared locals"))?;
        let mut emitter = FuncEmitter::new(&all_locals, fty.results().to_vec(), self.kb_mode)?;
        // Prepend original declared locals before synthetic locals in Function::new!
        let synth_locals = std::mem::take(&mut emitter.added_locals);
        for &ty in all_locals
            .get(fty.params().len()..)
            .ok_or(PostlinkError::InvalidIndex("orig declared slice"))?
        {
            emitter.added_locals.push(enc_valtype(ty)?);
        }
        debug_assert_eq!(emitter.added_locals.len(), orig_declared_len);
        emitter.added_locals.extend(synth_locals);

        emitter.build_function(&ast, end_poly)
    }

    fn build_fork_trampoline(
        &self,
        import_func_idx: u32,
        type_idx: u32,
    ) -> Result<Function, PostlinkError> {
        let fty = self.info.type_by_idx(type_idx)?;
        if fty.results() != [ValType::I64] {
            return Err(PostlinkError::UnsupportedForkConstruct(
                "fork import must return i64".into(),
            ));
        }
        let param_count =
            u32::try_from(fty.params().len()).map_err(|_| PostlinkError::InvalidIndex("params"))?;
        let kb_local = param_count;
        let mut func = Function::new([(1, EncValType::I64)]);

        match self.kb_mode {
            KbMode::StaticTls {
                tls_base_global,
                kb_offset_global,
            } => {
                func.instruction(&Instruction::GlobalGet(tls_base_global))
                    .instruction(&Instruction::GlobalGet(kb_offset_global))
                    .instruction(&Instruction::I64Add);
            }
            KbMode::ImportedGlobal(kb_global) => {
                func.instruction(&Instruction::GlobalGet(kb_global));
            }
        }
        func.instruction(&Instruction::LocalTee(kb_local))
            .instruction(&Instruction::I32Load(memarg_64(kernel_block::STATE_OFFSET, 2)))
            .instruction(&Instruction::I32Const(
                i32::try_from(kernel_block::STATE_REWINDING).unwrap_or(2),
            ))
            .instruction(&Instruction::I32Eq)
            .instruction(&Instruction::If(EncBlockType::Result(EncValType::I64)))
            // Rewinding leaf: clear state = STATE_RUNNING (0) and return FORK_RESULT_OFFSET
            .instruction(&Instruction::LocalGet(kb_local))
            .instruction(&Instruction::I32Const(
                i32::try_from(kernel_block::STATE_RUNNING).unwrap_or(0),
            ))
            .instruction(&Instruction::I32Store(memarg_64(kernel_block::STATE_OFFSET, 2)))
            .instruction(&Instruction::LocalGet(kb_local))
            .instruction(&Instruction::I64Load(memarg_64(
                kernel_block::FORK_RESULT_OFFSET,
                3,
            )))
            .instruction(&Instruction::Else);

        for p in 0..param_count {
            func.instruction(&Instruction::LocalGet(p));
        }
        func.instruction(&Instruction::Call(import_func_idx))
            .instruction(&Instruction::End)
            .instruction(&Instruction::End);
        Ok(func)
    }
}

/// Analyze and instrument a linked `wasm64-submilli-linux-musl` module for `fork()`.
pub fn rewrite_module(wasm: &[u8]) -> Result<(Vec<u8>, PostlinkStats), PostlinkError> {
    rewrite_module_with_paths(wasm, &[])
}

/// Analyze and instrument a linked static, PIE, or shared (`dylink.0`) module for `fork()`.
pub fn rewrite_module_with_paths(
    wasm: &[u8],
    lib_dirs: &[PathBuf],
) -> Result<(Vec<u8>, PostlinkStats), PostlinkError> {
    let (has_dylink, needed) = peek_dylink_info(wasm);
    let (external_fork_syms, known_so_exports) =
        if !has_dylink && lib_dirs.is_empty() && needed.is_empty() {
            (BTreeSet::new(), BTreeSet::new())
        } else {
            collect_external_fork_symbols(&needed, lib_dirs)
        };
    let info = analyze_module(wasm, has_dylink, &external_fork_syms, &known_so_exports)?;
    let defined_functions = info.defined_func_types.len();

    // If already instrumented by an earlier postlink invocation, validate and return as-is.
    if info
        .target_header
        .is_some_and(|h| (h.flags & TargetHeader::FLAG_FORK_INSTRUMENTED) != 0)
    {
        validate_wasm(wasm)?;
        return Ok((
            wasm.to_vec(),
            PostlinkStats {
                defined_functions,
                fork_reachable_functions: 0,
                instrumented: false,
            },
        ));
    }

    let (fork_funcs, fork_indirect_sigs) = compute_fork_reachable(&info)?;
    let imp_count = info.num_imported_funcs();
    let fork_defined_count = fork_funcs.iter().filter(|&&f| f >= imp_count).count();

    if fork_defined_count == 0 {
        validate_wasm(wasm)?;
        return Ok((
            wasm.to_vec(),
            PostlinkStats {
                defined_functions,
                fork_reachable_functions: 0,
                instrumented: false,
            },
        ));
    }

    let kb_mode = if let Some(kb_global) = info.imported_kb_global {
        KbMode::ImportedGlobal(kb_global)
    } else {
        let tls_base_global = info
            .tls_base_global
            .ok_or(PostlinkError::MissingGlobalExport("__tls_base"))?;
        let kb_offset_global = info
            .kb_offset_global
            .ok_or(PostlinkError::MissingGlobalExport("__submilli_kernel_block"))?;
        KbMode::StaticTls {
            tls_base_global,
            kb_offset_global,
        }
    };

    let mut trampolines = BTreeMap::new();
    let mut trampoline_list = Vec::new();
    let mut next_func_idx = imp_count
        .checked_add(
            u32::try_from(defined_functions)
                .map_err(|_| PostlinkError::InvalidIndex("def count"))?,
        )
        .ok_or(PostlinkError::InvalidIndex("next_func_idx"))?;

    for (idx, imp) in info.imported_funcs.iter().enumerate() {
        if imp.is_process_fork {
            let imp_u32 = u32::try_from(idx).map_err(|_| PostlinkError::InvalidIndex("imp"))?;
            trampolines.insert(imp_u32, next_func_idx);
            trampoline_list.push((imp_u32, imp.type_idx));
            next_func_idx = next_func_idx
                .checked_add(1)
                .ok_or(PostlinkError::InvalidIndex("trampoline idx"))?;
        }
    }

    let mut rewriter = ForkRewriter {
        info: &info,
        fork_funcs: &fork_funcs,
        fork_indirect_sigs: &fork_indirect_sigs,
        trampolines,
        trampoline_list,
        kb_mode,
    };

    let mut out_mod = Module::new();
    rewriter
        .parse_core_module(&mut out_mod, Parser::new(0), wasm)
        .map_err(|e| match e {
            reencode::Error::UserError(ue) => ue,
            other => PostlinkError::Reencode(format!("{other:?}")),
        })?;

    if info.has_dylink {
        let mut fork_exports_payload = String::new();
        for (name, func_idx) in &info.exported_funcs {
            if fork_funcs.contains(func_idx) {
                fork_exports_payload.push_str(name);
                fork_exports_payload.push('\n');
            }
        }
        if !fork_exports_payload.is_empty() {
            out_mod.section(&CustomSection {
                name: FORK_EXPORTS_SECTION_NAME.into(),
                data: fork_exports_payload.as_bytes().into(),
            });
        }
    }

    let out_bytes = out_mod.finish();
    validate_wasm(&out_bytes)?;

    Ok((
        out_bytes,
        PostlinkStats {
            defined_functions,
            fork_reachable_functions: fork_defined_count,
            instrumented: true,
        },
    ))
}

pub fn validate_wasm(wasm: &[u8]) -> Result<(), PostlinkError> {
    let mut validator = Validator::new_with_features(WasmFeatures::all());
    validator
        .validate_all(wasm)
        .map(|_| ())
        .map_err(PostlinkError::Validation)
}

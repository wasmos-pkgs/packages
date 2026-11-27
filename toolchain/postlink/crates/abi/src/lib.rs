//! `wasm64-submilli-linux-musl` ABI constants, checked guest addresses, and Linux `Errno`.

pub mod syscalls;

/// Linux `asm-generic` (`aarch64`) error numbers returned as negative `i64` from `wali.SYS_*`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(i32)]
pub enum Errno {
    Eperm = 1,
    Enoent = 2,
    Esrch = 3,
    Eintr = 4,
    Eio = 5,
    Enxio = 6,
    E2big = 7,
    Enoexec = 8,
    Ebadf = 9,
    Echild = 10,
    Eagain = 11,
    Enomem = 12,
    Eacces = 13,
    Efault = 14,
    Eexist = 17,
    Exdev = 18,
    Enodev = 19,
    Enotdir = 20,
    Eisdir = 21,
    Einval = 22,
    Enfile = 23,
    Emfile = 24,
    Enotty = 25,
    Efbig = 27,
    Enospc = 28,
    Espipe = 29,
    Erofs = 30,
    Epipe = 32,
    Erange = 34,
    Enosys = 38,
    Enotempty = 39,
    Eloop = 40,
    Eopnotsupp = 95,
    Etimedout = 110,
}

impl Errno {
    /// Encode as a negative Linux syscall return value.
    #[must_use]
    pub const fn to_syscall_ret(self) -> i64 {
        0_i64.saturating_sub(self as i64)
    }
}

/// Checked 64-bit guest virtual address in `memory64`.
/// Never cast to a host pointer.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct GuestAddr(u64);

impl GuestAddr {
    #[must_use]
    pub const fn new(raw: u64) -> Self {
        Self(raw)
    }

    #[must_use]
    pub const fn raw(self) -> u64 {
        self.0
    }

    /// Checked addition returning `Err(Errno::Efault)` on overflow.
    pub const fn checked_add(self, offset: u64) -> Result<Self, Errno> {
        match self.0.checked_add(offset) {
            Some(sum) => Ok(Self(sum)),
            None => Err(Errno::Efault),
        }
    }

    /// Validate that `[self, self + len)` does not overflow and lies strictly below `limit`.
    pub const fn validate_range(self, len: u64, limit: u64) -> Result<u64, Errno> {
        if len == 0 {
            return if self.0 <= limit {
                Ok(self.0)
            } else {
                Err(Errno::Efault)
            };
        }
        match self.0.checked_add(len) {
            Some(end) if end <= limit => Ok(end),
            _ => Err(Errno::Efault),
        }
    }
}

/// Custom section `submilli.target` payload (16 bytes).
pub const TARGET_SECTION_NAME: &str = "submilli.target";
pub const TARGET_MAGIC: [u8; 4] = *b"smli";
pub const TARGET_ABI_MAJOR: u16 = 1;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TargetHeader {
    pub major: u16,
    pub minor: u16,
    pub profile: u8,
    pub flags: u8,
}

impl TargetHeader {
    pub const FLAG_FORK_INSTRUMENTED: u8 = 1 << 0;

    pub fn decode(bytes: &[u8]) -> Result<Self, Errno> {
        let [m0, m1, m2, m3, maj0, maj1, min0, min1, profile, flags, r0, r1, r2, r3, r4, r5] =
            *bytes
        else {
            return Err(Errno::Enoexec);
        };
        if [m0, m1, m2, m3] != TARGET_MAGIC || [r0, r1, r2, r3, r4, r5] != [0; 6] {
            return Err(Errno::Enoexec);
        }
        let major = u16::from_le_bytes([maj0, maj1]);
        let minor = u16::from_le_bytes([min0, min1]);
        if major != TARGET_ABI_MAJOR || profile != 0 || (flags & !Self::FLAG_FORK_INSTRUMENTED) != 0
        {
            return Err(Errno::Enoexec);
        }
        Ok(Self {
            major,
            minor,
            profile,
            flags,
        })
    }

    #[must_use]
    pub const fn encode(self) -> [u8; 16] {
        let [maj0, maj1] = self.major.to_le_bytes();
        let [min0, min1] = self.minor.to_le_bytes();
        [
            TARGET_MAGIC[0],
            TARGET_MAGIC[1],
            TARGET_MAGIC[2],
            TARGET_MAGIC[3],
            maj0,
            maj1,
            min0,
            min1,
            self.profile,
            self.flags,
            0,
            0,
            0,
            0,
            0,
            0,
        ]
    }
}

/// Offsets within the 128-byte per-thread `kernel_block`.
pub mod kernel_block {
    pub const BYTES: u64 = 128;
    pub const STATE_OFFSET: u64 = 0;
    pub const SIGNAL_PENDING_OFFSET: u64 = 4;
    pub const FRAME_BUF_START_OFFSET: u64 = 8;
    pub const FRAME_BUF_END_OFFSET: u64 = 16;
    pub const FRAME_BUF_TOP_OFFSET: u64 = 24;
    pub const FORK_RESULT_OFFSET: u64 = 32;

    pub const STATE_RUNNING: u32 = 0;
    pub const STATE_UNWINDING: u32 = 1;
    pub const STATE_REWINDING: u32 = 2;
}

#[cfg(test)]
mod tests {
    use super::{Errno, GuestAddr, TargetHeader, TARGET_ABI_MAJOR};

    #[test]
    fn guest_addr_rejects_overflow_and_out_of_bounds() {
        let limit = 16_u64 << 30;
        assert_eq!(
            GuestAddr::new(0x1000).validate_range(0x1000, limit),
            Ok(0x2000)
        );
        assert_eq!(
            GuestAddr::new(u64::MAX.saturating_sub(7)).validate_range(16, limit),
            Err(Errno::Efault)
        );
        assert_eq!(
            GuestAddr::new(limit).validate_range(1, limit),
            Err(Errno::Efault)
        );
    }

    #[test]
    fn target_header_roundtrips_and_rejects_reserved_bits() {
        let header = TargetHeader {
            major: TARGET_ABI_MAJOR,
            minor: 0,
            profile: 0,
            flags: TargetHeader::FLAG_FORK_INSTRUMENTED,
        };
        let bytes = header.encode();
        assert_eq!(TargetHeader::decode(&bytes), Ok(header));

        let mut bad = bytes;
        if let Some(slot) = bad.get_mut(10) {
            *slot = 1;
        }
        assert_eq!(TargetHeader::decode(&bad), Err(Errno::Enoexec));
    }
}

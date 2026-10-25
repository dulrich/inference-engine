//! Shared test helpers for the server's unit tests: the committed `fixtures/` tree and a
//! self-cleaning temp dir (moved out of the deleted `record_tests.rs`, audit E1).
use std::path::Path;

pub(crate) fn fixtures(set: &str) -> String {
    crate::jsrt::path_join(env!("CARGO_MANIFEST_DIR"), &format!("../../fixtures/{set}"))
}

pub(crate) struct TempDir(pub String);
impl TempDir {
    pub(crate) fn new(tag: &str) -> TempDir {
        use std::sync::atomic::{AtomicUsize, Ordering};
        static N: AtomicUsize = AtomicUsize::new(0);
        let p = std::env::temp_dir().join(format!(
            "bd-{tag}-{}-{}",
            std::process::id(),
            N.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir_all(&p).unwrap();
        TempDir(p.to_string_lossy().into_owned())
    }
    pub(crate) fn write(&self, rel: &str, text: &str) {
        let p = Path::new(&self.0).join(rel);
        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
        std::fs::write(p, text).unwrap();
    }
}
impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

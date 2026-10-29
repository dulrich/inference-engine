//! A bare `--help` prints usage and exits 0 without reading the environment config or spawning
//! any codex child.
use std::process::Command;

#[test]
fn help_prints_usage_and_touches_nothing() {
    let dir = std::env::temp_dir().join(format!("cs-cli-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    for args in [&["--help"][..], &["-h"][..]] {
        let out = Command::new(env!("CARGO_BIN_EXE_codex-sidecar"))
            .args(args)
            .current_dir(&dir)
            .env_clear()
            .output()
            .expect("spawn codex-sidecar");
        assert_eq!(out.status.code(), Some(0), "{args:?}: {out:?}");
        let text = String::from_utf8_lossy(&out.stdout);
        assert!(text.contains("usage: codex-sidecar"), "{args:?}: {text}");
    }
    assert_eq!(
        std::fs::read_dir(&dir).unwrap().count(),
        0,
        "help created files"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn build_prints_one_build_id_line_and_touches_nothing() {
    let dir = std::env::temp_dir().join(format!("cs-cli-build-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    for flag in ["--build", "--version"] {
        let out = Command::new(env!("CARGO_BIN_EXE_codex-sidecar"))
            .arg(flag)
            .current_dir(&dir)
            .env_clear()
            .output()
            .expect("spawn codex-sidecar");
        assert_eq!(out.status.code(), Some(0), "{flag}: {out:?}");
        let text = String::from_utf8_lossy(&out.stdout);
        let id = text
            .strip_suffix('\n')
            .expect("one newline-terminated line");
        let hex = id.strip_suffix("-dirty").unwrap_or(id);
        assert!(
            id == "unknown" || (hex.len() == 40 && hex.bytes().all(|b| b.is_ascii_hexdigit())),
            "{flag}: {text:?}"
        );
    }
    assert_eq!(
        std::fs::read_dir(&dir).unwrap().count(),
        0,
        "build created files"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

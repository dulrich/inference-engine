//! SPA serving from `APP_WEB_DIR` (`apps/api/src/app.ts`'s `app.get("*")`): the decoded path
//! resolves to a file inside the web root or falls back to `index.html`; nothing outside is
//! ever served. Copied from radar_tracker's `spa.rs`; an undecodable path is `None` (Bun's
//! `decodeURIComponent` throws there: Hono's plain-text 500).
use std::path::{Path, PathBuf};

/// Resolve a raw request path to a file under `root` (else `index.html`); None when the path
/// does not percent-decode.
pub fn resolve(root: &Path, raw_path: &str) -> Option<PathBuf> {
    let index = root.join("index.html");
    if raw_path == "/" {
        return Some(index);
    }
    let decoded = crate::jsrt::decode_uri_component(raw_path)?;
    Some(resolve_decoded(root, &decoded, index))
}

fn resolve_decoded(root: &Path, decoded: &str, index: PathBuf) -> PathBuf {
    if decoded.contains('\0') {
        return index;
    }
    let mut parts: Vec<&str> = Vec::new();
    for segment in decoded.split('/') {
        match segment {
            "" | "." => {}
            ".." => {
                if parts.pop().is_none() {
                    return index; // would escape the web root
                }
            }
            s => parts.push(s),
        }
    }
    let mut candidate = root.to_path_buf();
    for p in &parts {
        candidate.push(p);
    }
    if !parts.is_empty() && candidate.is_file() {
        candidate
    } else {
        index
    }
}

pub fn content_type(path: &Path) -> &'static str {
    match path.extension().and_then(|e| e.to_str()).unwrap_or("") {
        "html" | "htm" => "text/html;charset=utf-8",
        "js" | "mjs" => "text/javascript;charset=utf-8",
        "css" => "text/css;charset=utf-8",
        "json" | "map" => "application/json;charset=utf-8",
        "webmanifest" => "application/manifest+json",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "webp" => "image/webp",
        "ico" => "image/x-icon",
        "woff2" => "font/woff2",
        "woff" => "font/woff",
        "txt" => "text/plain;charset=utf-8",
        _ => "application/octet-stream",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `apps/api/src/spa-fallback.test.ts` intent plus root-escape protection.
    #[test]
    fn never_escapes_the_root() {
        let root = std::env::temp_dir().join(format!("bd-spa-{}", std::process::id()));
        std::fs::create_dir_all(root.join("assets")).unwrap();
        std::fs::write(root.join("index.html"), "<html>").unwrap();
        std::fs::write(root.join("assets/app.js"), "x").unwrap();
        let index = root.join("index.html");
        let resolve = |root: &Path, p: &str| super::resolve(root, p).unwrap();
        assert_eq!(resolve(&root, "/"), index);
        assert_eq!(resolve(&root, "/assets/app.js"), root.join("assets/app.js"));
        assert_eq!(resolve(&root, "/some/route"), index);
        assert_eq!(resolve(&root, "/assets"), index);
        assert_eq!(resolve(&root, "/missing.js"), index);
        assert_eq!(resolve(&root, "/assets/missing.css"), index);
        for evil in [
            "/../x",
            "/%2e%2e",
            "/%2e%2e/%2e%2e/etc/passwd",
            "/..%2f..%2fetc%2fpasswd",
            "/assets/../../etc/passwd",
            "/%2e%2e/package.json",
            "/a%00b",
        ] {
            assert_eq!(resolve(&root, evil), index, "{evil}");
        }
        assert_eq!(super::resolve(&root, "/%E0%A4%A"), None);
        std::fs::remove_dir_all(&root).unwrap();
    }
}

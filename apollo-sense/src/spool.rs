use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

use crate::features::WindowFeatures;

fn hermes_home() -> PathBuf {
    match std::env::var("HERMES_HOME") {
        Ok(h) if !h.is_empty() => PathBuf::from(h),
        _ => {
            let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
            PathBuf::from(home).join(".hermes")
        }
    }
}

pub fn default_spool_path() -> PathBuf {
    hermes_home().join("audio_spool.jsonl")
}

pub fn default_location_spool_path() -> PathBuf {
    hermes_home().join("location_spool.jsonl")
}

pub fn row_json(window_start: &str, window_end: &str, features: &WindowFeatures) -> String {
    let mut histogram = String::from("{");
    for (i, (db, count)) in features.level_hist_db.iter().enumerate() {
        if i > 0 {
            histogram.push(',');
        }
        histogram.push_str(&format!("\"{db}\":{count}"));
    }
    histogram.push('}');
    format!(
        "{{\"window_start\":\"{window_start}\",\"window_end\":\"{window_end}\",\"avg_noise_db\":{:.4},\"peak_noise_db\":{:.4},\"level_hist_db\":{histogram},\"coverage_seconds\":{},\"sample_rate_hz\":{},\"schema\":\"v2\"}}",
        features.avg_noise_db, features.peak_noise_db, features.coverage_seconds, features.sample_rate_hz
    )
}

pub fn append_row(spool_path: &Path, line: &str) -> Result<(), String> {
    if let Some(parent) = spool_path.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("create spool dir: {e}"))?;
    }
    let mut buffer = String::with_capacity(line.len() + 1);
    buffer.push_str(line);
    buffer.push('\n');
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(spool_path)
        .map_err(|e| format!("open spool: {e}"))?;
    file.write_all(buffer.as_bytes())
        .map_err(|e| format!("write spool: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    fn sample_features() -> WindowFeatures {
        let mut level_hist_db = BTreeMap::new();
        level_hist_db.insert(-23i64, 40u32);
        level_hist_db.insert(-10, 17);
        WindowFeatures {
            avg_noise_db: -12.3,
            peak_noise_db: 4.8,
            level_hist_db,
            coverage_seconds: 57,
            sample_rate_hz: 44100,
            all_zero: false,
        }
    }

    #[test]
    fn row_json_is_v2_with_sorted_histogram() {
        let s = row_json("2026-07-02T09:00:00Z", "2026-07-02T09:01:00Z", &sample_features());
        assert!(s.starts_with('{') && s.ends_with('}'));
        assert!(s.contains("\"schema\":\"v2\""));
        assert!(s.contains("\"level_hist_db\":{\"-23\":40,\"-10\":17}"));
        assert!(s.contains("\"coverage_seconds\":57"));
        assert!(!s.contains("quiet_seconds"));
    }
}

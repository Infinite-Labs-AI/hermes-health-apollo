use std::collections::BTreeMap;

use crate::dsp;

pub struct WindowFeatures {
    pub avg_noise_db: f64,
    pub peak_noise_db: f64,
    pub level_hist_db: BTreeMap<i64, u32>,
    pub coverage_seconds: u32,
    pub sample_rate_hz: u32,
    pub all_zero: bool,
}

impl WindowFeatures {
    pub fn quiet_seconds_at(&self, threshold_db: f64) -> u32 {
        self.level_hist_db
            .iter()
            .filter(|(db, _)| (**db as f64) <= threshold_db)
            .map(|(_, count)| *count)
            .sum()
    }

    pub fn percentile_db(&self, fraction: f64) -> Option<i64> {
        let total: u32 = self.level_hist_db.values().sum();
        if total == 0 {
            return None;
        }
        let target = ((fraction * total as f64).ceil() as u32).max(1);
        let mut cumulative = 0u32;
        for (db, count) in &self.level_hist_db {
            cumulative += count;
            if cumulative >= target {
                return Some(*db);
            }
        }
        self.level_hist_db.keys().next_back().copied()
    }
}

pub fn compute_window(samples: &[f32], sample_rate: u32) -> WindowFeatures {
    let avg_noise_db = dsp::rms_db(samples);
    let peak_noise_db = dsp::peak_db(samples);

    let sub_len = sample_rate as usize;
    let mut level_hist_db: BTreeMap<i64, u32> = BTreeMap::new();
    let mut coverage_seconds = 0u32;
    if sub_len > 0 {
        let mut i = 0;
        while i + sub_len <= samples.len() {
            let db = dsp::rms_db(&samples[i..i + sub_len]);
            *level_hist_db.entry(db.round() as i64).or_insert(0) += 1;
            coverage_seconds += 1;
            i += sub_len;
        }
    }

    let all_zero = !samples.is_empty() && samples.iter().all(|s| *s == 0.0);

    WindowFeatures {
        avg_noise_db,
        peak_noise_db,
        level_hist_db,
        coverage_seconds,
        sample_rate_hz: sample_rate,
        all_zero,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn constant(amplitude: f32, samples: usize) -> Vec<f32> {
        vec![amplitude; samples]
    }

    #[test]
    fn silence_histogram_counts_as_quiet() {
        let sr = 1000;
        let f = compute_window(&constant(0.001, sr as usize * 5), sr);
        assert_eq!(f.coverage_seconds, 5);
        assert_eq!(f.level_hist_db.get(&-60), Some(&5));
        assert_eq!(f.quiet_seconds_at(-50.0), 5);
        assert!(f.avg_noise_db < -50.0);
    }

    #[test]
    fn loud_histogram_counts_no_quiet() {
        let sr = 1000;
        let f = compute_window(&constant(0.1, sr as usize * 5), sr);
        assert_eq!(f.level_hist_db.get(&-20), Some(&5));
        assert_eq!(f.quiet_seconds_at(-50.0), 0);
    }

    #[test]
    fn mixed_histogram_rethresholds_losslessly() {
        let sr = 1000;
        let mut samples = constant(0.001, sr as usize * 3);
        samples.extend(constant(0.1, sr as usize * 2));
        let f = compute_window(&samples, sr);
        assert_eq!(f.coverage_seconds, 5);
        assert_eq!(f.level_hist_db.get(&-60), Some(&3));
        assert_eq!(f.level_hist_db.get(&-20), Some(&2));
        assert_eq!(f.quiet_seconds_at(-50.0), 3);
        assert_eq!(f.quiet_seconds_at(-25.0), 3);
        assert_eq!(f.quiet_seconds_at(-20.0), 5);
    }

    #[test]
    fn exact_zero_window_is_flagged() {
        let sr = 1000;
        let f = compute_window(&constant(0.0, sr as usize * 2), sr);
        assert!(f.all_zero);
    }

    #[test]
    fn percentile_db_estimates_the_low_floor() {
        let mut level_hist_db = BTreeMap::new();
        level_hist_db.insert(-30i64, 8u32);
        level_hist_db.insert(-10, 2);
        let f = WindowFeatures {
            avg_noise_db: 0.0,
            peak_noise_db: 0.0,
            level_hist_db,
            coverage_seconds: 10,
            sample_rate_hz: 1000,
            all_zero: false,
        };
        assert_eq!(f.percentile_db(0.10), Some(-30));
        assert_eq!(f.percentile_db(0.90), Some(-10));
        assert_eq!(WindowFeatures::empty().percentile_db(0.10), None);
    }

    impl WindowFeatures {
        fn empty() -> Self {
            WindowFeatures {
                avg_noise_db: 0.0,
                peak_noise_db: 0.0,
                level_hist_db: BTreeMap::new(),
                coverage_seconds: 0,
                sample_rate_hz: 1000,
                all_zero: false,
            }
        }
    }
}

use std::time::{SystemTime, UNIX_EPOCH};

pub fn now_epoch_secs() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

pub fn iso_utc(epoch_secs: i64) -> String {
    let days = epoch_secs.div_euclid(86400);
    let rem = epoch_secs.rem_euclid(86400);
    let (year, month, day) = civil_from_days(days);
    let hours = rem / 3600;
    let minutes = (rem % 3600) / 60;
    let seconds = rem % 60;
    format!("{year:04}-{month:02}-{day:02}T{hours:02}:{minutes:02}:{seconds:02}Z")
}

pub fn epoch_from_iso(text: &str) -> Option<i64> {
    if text.len() < 20 {
        return None;
    }
    let year: i64 = text.get(0..4)?.parse().ok()?;
    let month: u32 = text.get(5..7)?.parse().ok()?;
    let day: u32 = text.get(8..10)?.parse().ok()?;
    let hour: i64 = text.get(11..13)?.parse().ok()?;
    let minute: i64 = text.get(14..16)?.parse().ok()?;
    let second: i64 = text.get(17..19)?.parse().ok()?;
    Some(days_from_civil(year, month, day) * 86400 + hour * 3600 + minute * 60 + second)
}

fn days_from_civil(year: i64, month: u32, day: u32) -> i64 {
    let year = if month <= 2 { year - 1 } else { year };
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let yoe = year - era * 400;
    let month = month as i64;
    let doy = (153 * (if month > 2 { month - 3 } else { month + 9 }) + 2) / 5 + day as i64 - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let year = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let month = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if month <= 2 { year + 1 } else { year }, month, day)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unix_epoch() {
        assert_eq!(iso_utc(0), "1970-01-01T00:00:00Z");
    }

    #[test]
    fn year_2000() {
        assert_eq!(iso_utc(946684800), "2000-01-01T00:00:00Z");
        assert_eq!(iso_utc(946730096), "2000-01-01T12:34:56Z");
    }

    #[test]
    fn leap_day_2000() {
        assert_eq!(iso_utc(951782400), "2000-02-29T00:00:00Z");
    }

    #[test]
    fn day_prefix_is_the_date() {
        assert_eq!(&iso_utc(946730096)[..10], "2000-01-01");
    }

    #[test]
    fn epoch_from_iso_round_trips() {
        for epoch in [0, 946684800, 946730096, 951782400, 1782045296] {
            assert_eq!(epoch_from_iso(&iso_utc(epoch)), Some(epoch));
        }
        assert_eq!(epoch_from_iso("2000-01-01T12:34:56Z"), Some(946730096));
        assert_eq!(epoch_from_iso("bad"), None);
    }
}

use std::io::Read;
use std::path::Path;

use crate::clock;
use crate::spool;

const CLUSTER_RADIUS_M: f64 = 100.0;
const MIN_VISIT_SECONDS: i64 = 120;
const EARTH_RADIUS_M: f64 = 6_371_000.0;

pub struct Ping {
    pub timestamp: String,
    pub lat: f64,
    pub lng: f64,
}

pub struct VisitRow {
    pub location_id: String,
    pub arrival_at: String,
    pub departure_at: String,
    pub centroid_lat_coarse: f64,
    pub centroid_lng_coarse: f64,
    pub radius_meters: f64,
}

struct Cluster {
    id: String,
    lat: f64,
    lng: f64,
    count: f64,
    radius_m: f64,
}

pub fn cluster_pings(pings: &[Ping]) -> Vec<VisitRow> {
    let mut clusters: Vec<Cluster> = Vec::new();
    let mut visits: Vec<VisitRow> = Vec::new();
    let mut open: Option<(usize, String, String)> = None;

    for ping in pings {
        let index = assign(&mut clusters, ping);
        match open.as_mut() {
            None => open = Some((index, ping.timestamp.clone(), ping.timestamp.clone())),
            Some((current, _arrival, last)) if *current == index => {
                *last = ping.timestamp.clone();
            }
            Some((current, arrival, last)) => {
                maybe_emit(&mut visits, &clusters[*current], arrival, last);
                open = Some((index, ping.timestamp.clone(), ping.timestamp.clone()));
            }
        }
    }
    visits
}

fn assign(clusters: &mut Vec<Cluster>, ping: &Ping) -> usize {
    let mut best: Option<(usize, f64)> = None;
    for (i, cluster) in clusters.iter().enumerate() {
        let distance = haversine_m(cluster.lat, cluster.lng, ping.lat, ping.lng);
        if distance <= CLUSTER_RADIUS_M && best.map_or(true, |(_, b)| distance < b) {
            best = Some((i, distance));
        }
    }
    if let Some((i, _)) = best {
        let cluster = &mut clusters[i];
        cluster.count += 1.0;
        cluster.lat += (ping.lat - cluster.lat) / cluster.count;
        cluster.lng += (ping.lng - cluster.lng) / cluster.count;
        let spread = haversine_m(cluster.lat, cluster.lng, ping.lat, ping.lng);
        if spread > cluster.radius_m {
            cluster.radius_m = spread;
        }
        i
    } else {
        clusters.push(Cluster {
            id: location_id(clusters.len()),
            lat: ping.lat,
            lng: ping.lng,
            count: 1.0,
            radius_m: 0.0,
        });
        clusters.len() - 1
    }
}

fn maybe_emit(visits: &mut Vec<VisitRow>, cluster: &Cluster, arrival: &str, departure: &str) {
    let duration = match (clock::epoch_from_iso(arrival), clock::epoch_from_iso(departure)) {
        (Some(a), Some(d)) => d - a,
        _ => 0,
    };
    if duration < MIN_VISIT_SECONDS {
        return;
    }
    visits.push(VisitRow {
        location_id: cluster.id.clone(),
        arrival_at: arrival.to_string(),
        departure_at: departure.to_string(),
        centroid_lat_coarse: round_coarse(cluster.lat),
        centroid_lng_coarse: round_coarse(cluster.lng),
        radius_meters: (cluster.radius_m * 10.0).round() / 10.0,
    });
}

fn location_id(index: usize) -> String {
    let mut n = index;
    let mut suffix = String::new();
    loop {
        suffix.insert(0, (b'A' + (n % 26) as u8) as char);
        if n < 26 {
            break;
        }
        n = n / 26 - 1;
    }
    format!("Location_{suffix}")
}

fn round_coarse(value: f64) -> f64 {
    (value * 1000.0).round() / 1000.0
}

fn haversine_m(lat1: f64, lng1: f64, lat2: f64, lng2: f64) -> f64 {
    let phi1 = lat1.to_radians();
    let phi2 = lat2.to_radians();
    let dphi = (lat2 - lat1).to_radians();
    let dlambda = (lng2 - lng1).to_radians();
    let a = (dphi / 2.0).sin().powi(2)
        + phi1.cos() * phi2.cos() * (dlambda / 2.0).sin().powi(2);
    2.0 * EARTH_RADIUS_M * a.sqrt().asin()
}

pub fn row_json(visit: &VisitRow) -> String {
    format!(
        "{{\"location_id\":\"{}\",\"arrival_at\":\"{}\",\"departure_at\":\"{}\",\"centroid_lat_coarse\":{:.3},\"centroid_lng_coarse\":{:.3},\"radius_meters\":{:.1},\"schema\":\"v1\"}}",
        visit.location_id,
        visit.arrival_at,
        visit.departure_at,
        visit.centroid_lat_coarse,
        visit.centroid_lng_coarse,
        visit.radius_meters
    )
}

fn parse_ping(line: &str) -> Option<Ping> {
    let mut fields = line.split_whitespace();
    let timestamp = fields.next()?.to_string();
    let lat: f64 = fields.next()?.parse().ok()?;
    let lng: f64 = fields.next()?.parse().ok()?;
    Some(Ping { timestamp, lat, lng })
}

pub fn run_cluster(pings_path: Option<&Path>, spool_path: &Path) -> Result<usize, String> {
    let content = match pings_path {
        Some(path) => std::fs::read_to_string(path).map_err(|e| format!("read pings: {e}"))?,
        None => {
            let mut buffer = String::new();
            std::io::stdin()
                .read_to_string(&mut buffer)
                .map_err(|e| format!("read stdin: {e}"))?;
            buffer
        }
    };
    let pings: Vec<Ping> = content.lines().filter_map(parse_ping).collect();
    let visits = cluster_pings(&pings);
    for visit in &visits {
        spool::append_row(spool_path, &row_json(visit))?;
    }
    Ok(visits.len())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ping(timestamp: &str, lat: f64, lng: f64) -> Ping {
        Ping { timestamp: timestamp.to_string(), lat, lng }
    }

    #[test]
    fn location_ids_are_lettered() {
        assert_eq!(location_id(0), "Location_A");
        assert_eq!(location_id(1), "Location_B");
        assert_eq!(location_id(25), "Location_Z");
    }

    #[test]
    fn far_points_are_distinct_clusters() {
        assert!(haversine_m(37.4219, -122.0841, 37.4300, -122.0841) > CLUSTER_RADIUS_M);
        assert!(haversine_m(37.4219, -122.0841, 37.42191, -122.08411) < CLUSTER_RADIUS_M);
    }

    #[test]
    fn emits_completed_visits_only() {
        // Stay at A (10 min), move to B (10 min), return to A (ongoing).
        let mut pings = Vec::new();
        for m in 0..=10 {
            pings.push(ping(&format!("2026-07-02T09:{m:02}:00Z"), 37.4219, -122.0841));
        }
        for m in 15..=25 {
            pings.push(ping(&format!("2026-07-02T09:{m:02}:00Z"), 37.4300, -122.0841));
        }
        for m in 30..=35 {
            pings.push(ping(&format!("2026-07-02T09:{m:02}:00Z"), 37.4219, -122.0841));
        }
        let visits = cluster_pings(&pings);
        // A(first stay) and B are completed; the final return to A is still open -> not emitted.
        assert_eq!(visits.len(), 2);
        assert_eq!(visits[0].location_id, "Location_A");
        assert_eq!(visits[0].arrival_at, "2026-07-02T09:00:00Z");
        assert_eq!(visits[0].departure_at, "2026-07-02T09:10:00Z");
        assert_eq!(visits[1].location_id, "Location_B");
    }

    #[test]
    fn short_passthrough_is_filtered() {
        let pings = vec![
            ping("2026-07-02T09:00:00Z", 37.4219, -122.0841),
            ping("2026-07-02T09:00:30Z", 37.4219, -122.0841),
            // one blip far away, then straight back -> under MIN_VISIT_SECONDS
            ping("2026-07-02T09:01:00Z", 37.4300, -122.0841),
            ping("2026-07-02T09:05:00Z", 37.4219, -122.0841),
            ping("2026-07-02T09:10:00Z", 37.4219, -122.0841),
        ];
        let visits = cluster_pings(&pings);
        // The A-then-B move completes an A visit of only 60s (<120) -> filtered.
        // The B visit is 0s -> filtered. Final A is open. So zero emitted.
        assert_eq!(visits.len(), 0);
    }
}

// k6 load test: a flash sale where 300 virtual users all rush the same event.
// Run: k6 run -e BASE=http://localhost:8000 bench/k6_booking_rush.js
// Expected: exactly one 201 per seat, everyone else gets 409, and zero 5xx errors.
import http from "k6/http";
import { check } from "k6";
import { Counter } from "k6/metrics";

const BASE = __ENV.BASE || "http://localhost:8000";
const won = new Counter("seats_won");
const lost = new Counter("seats_lost");

export const options = {
  scenarios: { rush: { executor: "per-vu-iterations", vus: 300, iterations: 1, maxDuration: "60s" } },
  thresholds: { http_req_failed: ["rate<0.01"], "http_req_duration{name:hold}": ["p(95)<800"] },
};

export function setup() {
  const ev = http.get(`${BASE}/events`).json().items[0];
  const seats = http.get(`${BASE}/events/${ev.id}/seats`).json().slice(0, 10);
  return { eventId: ev.id, seatIds: seats.map((s) => s.id) };
}

export default function (data) {
  const email = `vu${__VU}-${Date.now()}@load.test`;
  const reg = http.post(`${BASE}/auth/register`, JSON.stringify({ email, name: "vu", password: "password123" }),
    { headers: { "Content-Type": "application/json" } });
  const token = reg.json("access_token");
  const seatId = data.seatIds[__VU % data.seatIds.length]; // 30 users per seat
  const res = http.post(`${BASE}/events/${data.eventId}/seats/${seatId}/hold`, null, {
    headers: { Authorization: `Bearer ${token}`, "Idempotency-Key": `k6-${__VU}-${Date.now()}` },
    tags: { name: "hold" },
  });
  check(res, { "201 or 409": (r) => r.status === 201 || r.status === 409 });
  if (res.status === 201) won.add(1); else if (res.status === 409) lost.add(1);
}

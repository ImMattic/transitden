import { describe, it, expect } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../mocks/handlers";
import { ApiError, fetchVehicles, fetchHistorical, exportUrl, tripExportUrl } from "@/lib/api";

describe("apiFetch", () => {
  it("returns parsed JSON on 200", async () => {
    const data = await fetchVehicles();
    expect(data.vehicles).toBeDefined();
    expect(Array.isArray(data.vehicles)).toBe(true);
  });

  it("throws an error on non-ok response", async () => {
    server.use(
      http.get("/api/v1/realtime/vehicles", () =>
        HttpResponse.json({ detail: "server error" }, { status: 500 })
      )
    );
    await expect(fetchVehicles()).rejects.toThrow("API 500");
  });

  it("throws on 404", async () => {
    server.use(
      http.get("/api/v1/realtime/vehicles", () =>
        new HttpResponse(null, { status: 404 })
      )
    );
    await expect(fetchVehicles()).rejects.toThrow("API 404");
  });

  it("throws ApiError carrying the status code (so 4xx is not retried)", async () => {
    server.use(
      http.get("/api/v1/realtime/vehicles", () =>
        HttpResponse.json({ detail: "rate limit exceeded" }, { status: 429 })
      )
    );
    const err = await fetchVehicles().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(429);
  });
});

describe("fetchHistorical query string", () => {
  it("includes route_id in query string when provided", async () => {
    let capturedUrl = "";
    server.use(
      http.get("/api/v1/historical/vehicles", ({ request }) => {
        capturedUrl = request.url;
        return HttpResponse.json({
          start: "",
          end: "",
          page: 1,
          limit: 200,
          returned: 0,
          total: 0,
          total_pages: 0,
          vehicles: [],
        });
      })
    );
    await fetchHistorical({ route_id: "R1", limit: 50, page: 2 });
    const url = new URL(capturedUrl);
    expect(url.searchParams.get("route_id")).toBe("R1");
    expect(url.searchParams.get("limit")).toBe("50");
    expect(url.searchParams.get("page")).toBe("2");
  });

  it("omits route_id when not provided", async () => {
    let capturedUrl = "";
    server.use(
      http.get("/api/v1/historical/vehicles", ({ request }) => {
        capturedUrl = request.url;
        return HttpResponse.json({
          start: "",
          end: "",
          page: 1,
          limit: 200,
          returned: 0,
          total: 0,
          total_pages: 0,
          vehicles: [],
        });
      })
    );
    await fetchHistorical({ limit: 10 });
    const url = new URL(capturedUrl);
    expect(url.searchParams.has("route_id")).toBe(false);
    expect(url.searchParams.get("limit")).toBe("10");
  });
});

describe("exportUrl", () => {
  it("builds correct URL for CSV format", () => {
    const url = exportUrl({ format: "csv" });
    expect(url).toContain("/api/v1/export/vehicles");
    expect(url).toContain("format=csv");
  });

  it("includes route_id and limit when provided", () => {
    const url = exportUrl({ format: "json", route_id: "R1", limit: 100 });
    expect(url).toContain("route_id=R1");
    expect(url).toContain("limit=100");
    expect(url).toContain("format=json");
  });
});

describe("tripExportUrl", () => {
  it("carries the trip page's own params", () => {
    const url = tripExportUrl("FF 1505", {
      trip_id: "T1",
      start: "2026-09-28T14:00:00Z",
      end: "2026-09-28T15:00:00Z",
    });
    expect(url).toContain("/api/v1/export/trip?");
    const qs = new URL(url, "http://x").searchParams;
    expect(qs.get("vehicle_label")).toBe("FF 1505");
    expect(qs.get("trip_id")).toBe("T1");
    expect(qs.get("start")).toBe("2026-09-28T14:00:00Z");
    expect(qs.get("end")).toBe("2026-09-28T15:00:00Z");
  });

  it("omits bounds it wasn't given, so a live trip exports up to now", () => {
    const qs = new URL(tripExportUrl("101", { trip_id: "T1" }), "http://x").searchParams;
    expect(qs.has("end")).toBe(false);
    expect(qs.has("start")).toBe(false);
  });
});

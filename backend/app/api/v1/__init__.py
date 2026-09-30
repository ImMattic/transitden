from fastapi import APIRouter

from app.api.v1 import realtime, historical, routes, stops, stats, export, trip_export, analytics, vehicles, meta, sports

router = APIRouter(prefix="/api/v1")
router.include_router(meta.router)
router.include_router(sports.router)
router.include_router(realtime.router)
router.include_router(historical.router)
router.include_router(routes.router)
router.include_router(stops.router)
router.include_router(stats.router)
router.include_router(analytics.router)
router.include_router(export.router)
router.include_router(trip_export.router)
router.include_router(vehicles.router)

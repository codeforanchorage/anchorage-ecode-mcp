"""Anchorage GIS plugin implementation for OpenContext.

This plugin provides access to the Municipality of Anchorage GIS Gallery
and spatial data via the ArcGIS Portal REST API. It exposes tools for
discovering maps, apps, and spatial datasets published by MOA GIS.
"""

import asyncio
import json
import logging
import re
import time
from collections import OrderedDict, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from core.interfaces import DataPlugin, PluginType, ToolDefinition, ToolResult
from plugins.anchorage_gis.config_schema import AnchorageGISPluginConfig
from plugins.arcgis.where_validator import (
    OrderByValidator,
    OutFieldsValidator,
    WhereValidator,
)

logger = logging.getLogger(__name__)


class AnchorageGISPlugin(DataPlugin):
    """Plugin for accessing Municipality of Anchorage GIS data.

    Uses the ArcGIS Portal REST API to search the curated public gallery
    and the organization's spatial layers, retrieve item details, inspect
    Feature Service schemas, and query Feature Service records.
    """

    plugin_name = "anchorage_gis"
    plugin_type = PluginType.OPEN_DATA
    plugin_version = "1.0.0"

    GALLERY_APP_TYPES = [
        "Web Experience",
        "Web Mapping Application",
        "StoryMap",
        "Dashboard",
        "Hub Site Application",
        "Instant App",
    ]
    LAYER_TYPES = [
        "Feature Service",
        "Map Service",
        "Image Service",
        "Tile Layer",
        "Vector Tile Service",
        "WFS",
        "WMS",
    ]
    DATA_TYPES = [
        "CSV",
        "GeoJSON",
        "Shapefile",
        "File Geodatabase",
        "Web Map",
        "Web Scene",
    ]
    QUERYABLE_TYPES = {
        "Feature Service",
        "Map Service",
        "Feature Layer",
        "Table",
    }

    # Host suffixes we will make outbound HTTP calls to. Anything else is
    # rejected to prevent SSRF via user-supplied service URLs or item URLs
    # returned from search results. Covers Esri-hosted (*.arcgis.com) and
    # MOA on-prem GIS (*.muni.org).
    ALLOWED_HOST_SUFFIXES = (".arcgis.com", ".muni.org")

    ITEM_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.plugin_config: Optional[AnchorageGISPluginConfig] = None
        self.client: Optional[httpx.AsyncClient] = None
        # LRU {(item_id, group_by_field, agg_where): (expires_epoch, polygons)}.
        # Bounded to prevent memory exhaustion via agg_where variants that
        # miss the cache (e.g. "1=1 AND 1=1" vs "1=1 AND 2=2").
        self._agg_layer_cache: "OrderedDict[Tuple[str, str, str], Tuple[float, List[Dict[str, Any]]]]" = OrderedDict()

    async def initialize(self) -> bool:
        try:
            self.plugin_config = AnchorageGISPluginConfig(**self.config)
            self.client = httpx.AsyncClient(
                timeout=self.plugin_config.timeout,
            )

            # Test connectivity with a minimal search
            resp = await self.client.get(
                f"{self.plugin_config.portal_base_url}/search",
                params={
                    "q": f"orgid:{self.plugin_config.org_id}",
                    "f": "json",
                    "num": "1",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise RuntimeError(
                    data["error"].get("message", str(data["error"]))
                )

            self._initialized = True
            logger.info(
                f"Anchorage GIS plugin initialized successfully for "
                f"{self.plugin_config.city_name}"
            )
            return True

        except Exception as e:
            logger.error(
                f"Failed to initialize Anchorage GIS plugin: {e}", exc_info=True
            )
            return False

    async def shutdown(self) -> None:
        if self.client:
            await self.client.aclose()
            self.client = None
        self._initialized = False
        logger.info("Anchorage GIS plugin shut down")

    # ── Portal search helpers ─────────────────────────────────────────────

    async def _run_search(self, q: str, limit: int) -> List[Dict[str, Any]]:
        """Run a search against the ArcGIS Portal REST API."""
        params = {
            "q": q,
            "f": "json",
            "num": str(limit),
            "sortField": "relevance",
            "sortOrder": "desc",
        }
        resp = await self.client.get(
            f"{self.plugin_config.portal_base_url}/search",
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(
                data["error"].get("message", str(data["error"]))
            )
        return data.get("results", [])

    async def _search_gallery(
        self, query: str, limit: int
    ) -> List[Dict[str, Any]]:
        """Search within the curated gallery group."""
        clauses = [f"group:{self.plugin_config.gallery_group_id}"]
        if query:
            clauses.append(query)
        return await self._run_search(" AND ".join(clauses), limit)

    async def _search_org_layers(
        self, query: str, item_types: List[str], limit: int
    ) -> List[Dict[str, Any]]:
        """Search the organization's spatial layers."""
        type_filter = " OR ".join(f'type:"{t}"' for t in item_types)
        clauses = [f"orgid:{self.plugin_config.org_id}", f"({type_filter})"]
        if query:
            clauses.append(query)
        return await self._run_search(" AND ".join(clauses), limit)

    # ── Formatters ────────────────────────────────────────────────────────

    @property
    def _portal_home(self) -> str:
        """Portal home URL (without /sharing/rest)."""
        return self.plugin_config.portal_base_url.replace("/sharing/rest", "")

    def _item_portal_url(self, item: Dict[str, Any]) -> str:
        url = item.get("url", "")
        item_type = item.get("type", "")
        item_id = item.get("id", "")
        if url and item_type in self.GALLERY_APP_TYPES:
            return url
        return f"{self._portal_home}/home/item.html?id={item_id}"

    def _format_summary(self, item: Dict[str, Any]) -> str:
        title = item.get("title", "Untitled")
        item_type = item.get("type", "Unknown")
        snippet = (item.get("snippet") or "").strip()
        tags = item.get("tags", [])
        item_id = item.get("id", "")
        url = self._item_portal_url(item)

        lines = [f"**{title}**  _{item_type}_ — ID: `{item_id}`"]
        if snippet:
            lines.append(snippet)
        if tags:
            lines.append(f"Tags: {', '.join(tags[:6])}")
        lines.append(url)
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _ms_to_date(ms: Any) -> str:
        if ms:
            try:
                return datetime.fromtimestamp(
                    int(ms) / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d")
            except (ValueError, TypeError, OSError):
                pass
        return "Unknown"

    def _format_details(self, item: Dict[str, Any]) -> str:
        title = item.get("title", "Untitled")
        item_type = item.get("type", "Unknown")
        item_id = item.get("id", "")
        snippet = (item.get("snippet") or "").strip()
        description = (
            item.get("description") or "No description available."
        ).strip()
        tags = item.get("tags", [])
        categories = item.get("categories", [])
        owner = item.get("owner", "")
        access = item.get("access", "")
        url = item.get("url", "")
        num_views = item.get("numViews", 0)
        created = self._ms_to_date(item.get("created"))
        modified = self._ms_to_date(item.get("modified"))

        extent = item.get("extent") or []
        extent_str = ""
        if len(extent) == 2:
            try:
                extent_str = (
                    f"SW {extent[0][1]:.4f}\u00b0N {extent[0][0]:.4f}\u00b0E  "
                    f"NE {extent[1][1]:.4f}\u00b0N {extent[1][0]:.4f}\u00b0E"
                )
            except (IndexError, TypeError):
                pass

        lines = [
            f"## {title}",
            f"**Type:** {item_type}  |  **ID:** `{item_id}`",
            f"**Owner:** {owner}  |  **Access:** {access}  |  **Views:** {num_views:,}",
            f"**Created:** {created}  |  **Modified:** {modified}",
            "",
            "### Summary",
            snippet or "_No summary._",
            "",
            "### Description",
            description,
        ]
        if tags:
            lines += ["", f"**Tags:** {', '.join(tags)}"]
        if categories:
            lines += [f"**Categories:** {', '.join(categories)}"]
        if extent_str:
            lines += [f"**Spatial Extent:** {extent_str}"]
        if url:
            lines += [f"**Service/App URL:** {url}"]
        lines += [
            f"**Portal Page:** {self._portal_home}/home/item.html?id={item_id}"
        ]
        return "\n".join(lines)

    # Upper bound on how many chars of a single feature's geometry we
    # dump into the response. Simplified polygons are usually well
    # under this; anything larger gets truncated with a clear marker.
    GEOMETRY_STR_MAX = 600

    def _format_query_results(
        self,
        records: List[Dict[str, Any]],
        limit: int,
        total_count: Optional[int] = None,
        date_fields: Optional[set] = None,
    ) -> str:
        if not records:
            return "No records returned."

        count_part = f"{len(records)}"
        if total_count is not None:
            count_part += f" of {total_count:,} total"
        lines = [f"Returned {count_part} record(s) (limit: {limit}):\n"]
        for i, record in enumerate(records, 1):
            lines.append(f"Record {i}:")
            geometry = record.get("__geometry__")
            for key, value in record.items():
                if key == "__geometry__":
                    continue
                if date_fields and key in date_fields and value is not None:
                    value = self._ms_to_date(value)
                lines.append(f"  {key}: {value}")
            if geometry is not None:
                geom_str = json.dumps(geometry, separators=(",", ":"))
                if len(geom_str) > self.GEOMETRY_STR_MAX:
                    truncated = geom_str[: self.GEOMETRY_STR_MAX]
                    geom_str = (
                        f"{truncated}... "
                        f"(truncated, {len(geom_str)} chars total; "
                        f"server-side simplified to "
                        f"~{self.GEOMETRY_SIMPLIFY_OFFSET_DEG}° "
                        f"≈ 5.5m)"
                    )
                lines.append(f"  geometry (GeoJSON, WGS84): {geom_str}")
            lines.append("")
        return "\n".join(lines)

    # ── DataPlugin interface methods ──────────────────────────────────────

    async def search_datasets(
        self, query: str, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Search gallery and org layers for a topic."""
        gallery, layers = await asyncio.gather(
            self._search_gallery(query, limit),
            self._search_org_layers(
                query, self.LAYER_TYPES + self.DATA_TYPES, limit
            ),
        )
        return gallery + layers

    async def get_dataset(self, dataset_id: str) -> Dict[str, Any]:
        """Get item details by ArcGIS item ID."""
        dataset_id = self._validate_item_id(dataset_id)
        resp = await self.client.get(
            f"{self.plugin_config.portal_base_url}/content/items/{dataset_id}",
            params={"f": "json"},
        )
        resp.raise_for_status()
        item = resp.json()
        if "error" in item:
            raise RuntimeError(
                item["error"].get("message", str(item["error"]))
            )
        return item

    # Cap on records when geometry is requested — polygons can be
    # orders of magnitude larger than attribute rows, so we keep this
    # much tighter than the no-geometry cap of 1000.
    GEOMETRY_LIMIT_CAP = 50

    # Server-side simplification tolerance in the output SR's units.
    # We pin outSR=4326 when returnGeometry=true, so this is in decimal
    # degrees: 0.00005° ≈ 5.5m at the equator. Fine for MCP-scale
    # reasoning about shape, keeps payloads manageable.
    GEOMETRY_SIMPLIFY_OFFSET_DEG = 0.00005

    async def query_data(
        self,
        resource_id: str,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 100,
        return_geometry: bool = False,
    ) -> List[Dict[str, Any]]:
        """Query records from a Feature Service by item ID.

        Two-hop resolution: looks up item to get service URL, then queries it.

        When return_geometry=True, the response format switches to GeoJSON
        (f=geojson), geometry is auto-simplified to
        ~GEOMETRY_SIMPLIFY_OFFSET_DEG, and `limit` is capped at
        GEOMETRY_LIMIT_CAP. Each returned record carries a `__geometry__`
        key holding the GeoJSON geometry object.
        """
        if limit < 1:
            raise ValueError(f"limit must be at least 1 (got {limit})")

        item = await self.get_dataset(resource_id)
        service_url = item.get("url", "")
        item_type = item.get("type", "")

        if not service_url:
            raise ValueError(
                f"Item {resource_id} does not have a queryable service URL"
            )
        if item_type and item_type not in self.QUERYABLE_TYPES:
            raise ValueError(
                f"Item type '{item_type}' is not queryable. "
                f"query_data supports: {', '.join(sorted(self.QUERYABLE_TYPES))}."
            )

        where_clause = filters.get("where", "1=1") if filters else "1=1"
        where_clause = WhereValidator.validate(where_clause)
        out_fields = OutFieldsValidator.validate(
            filters.get("out_fields", "*") if filters else "*"
        )
        order_by = OrderByValidator.validate(
            filters.get("order_by") or "" if filters else ""
        )

        service_url = self._ensure_layer_url(service_url)
        self._validate_service_url(service_url)
        query_url = f"{service_url}/query"

        max_records = (
            self.GEOMETRY_LIMIT_CAP if return_geometry else 1000
        )
        effective_limit = min(limit, max_records)
        params: Dict[str, Any] = {
            "where": where_clause,
            "outFields": out_fields,
            "resultRecordCount": effective_limit,
            "f": "geojson" if return_geometry else "json",
            "returnGeometry": "true" if return_geometry else "false",
        }
        if return_geometry:
            # Pin output SR so callers can't swap coordinate systems;
            # keeps the simplification offset in known units.
            params["outSR"] = "4326"
            params["maxAllowableOffset"] = str(
                self.GEOMETRY_SIMPLIFY_OFFSET_DEG
            )
        if order_by:
            params["orderByFields"] = order_by

        try:
            resp = await self.client.get(query_url, params=params)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Feature Service query error (HTTP {e.response.status_code}): "
                f"{e.response.text}"
            ) from e

        try:
            data = resp.json()
        except Exception as json_err:
            content_type = resp.headers.get("content-type", "")
            raise ValueError(
                f"Feature Service returned non-JSON response "
                f"(content-type: {content_type}). The item URL may not "
                f"point to a queryable ArcGIS Feature Service."
            ) from json_err

        error_in_body = data.get("error")
        if error_in_body:
            code = error_in_body.get("code", "unknown")
            msg = error_in_body.get("message", "Unknown error")
            details = error_in_body.get("details", [])
            detail_str = "; ".join(details) if details else ""
            raise RuntimeError(
                f"Feature Service query failed (code {code}): {msg}"
                + (f" — {detail_str}" if detail_str else "")
            )

        features = data.get("features", [])
        if return_geometry:
            # f=geojson returns FeatureCollection with
            # {type,geometry,properties} features.
            return [
                {
                    **(f.get("properties") or {}),
                    "__geometry__": f.get("geometry"),
                }
                for f in features
            ]
        return [f.get("attributes", {}) for f in features]

    async def spatial_query_point(
        self,
        resource_id: str,
        lon: float,
        lat: float,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Find polygon features in a Feature Service that contain a point.

        Uses ArcGIS REST spatialRel=Intersects with a point geometry in
        WGS84 (EPSG:4326). Input SR is pinned server-side so callers
        cannot supply an arbitrary CRS. Returns attributes only; geometry
        is suppressed to keep payloads small.
        """
        if limit < 1:
            raise ValueError(f"limit must be at least 1 (got {limit})")

        lon_f, lat_f = self._validate_lonlat(lon, lat)

        item = await self.get_dataset(resource_id)
        service_url = item.get("url", "")
        item_type = item.get("type", "")

        if not service_url:
            raise ValueError(
                f"Item {resource_id} does not have a queryable service URL"
            )
        if item_type and item_type not in self.QUERYABLE_TYPES:
            raise ValueError(
                f"Item type '{item_type}' is not queryable. "
                f"spatial_query_point supports: "
                f"{', '.join(sorted(self.QUERYABLE_TYPES))}."
            )

        where_clause = WhereValidator.validate(
            (filters or {}).get("where", "1=1")
        )
        out_fields = OutFieldsValidator.validate(
            (filters or {}).get("out_fields", "*")
        )

        service_url = self._ensure_layer_url(service_url)
        self._validate_service_url(service_url)

        # Pre-check: layer must be polygon-type for point-in-polygon
        # to make sense. Fail loudly rather than silently returning
        # whatever Intersects happens to hit on a points/lines layer.
        meta_resp = await self.client.get(
            service_url, params={"f": "json"}
        )
        meta_resp.raise_for_status()
        meta = meta_resp.json()
        if "error" in meta:
            raise RuntimeError(
                meta["error"].get("message", str(meta["error"]))
            )
        geom_type = meta.get("geometryType", "")
        if geom_type not in (
            "esriGeometryPolygon",
            "esriGeometryMultiPatch",
        ):
            raise ValueError(
                f"spatial_query_point requires a polygon layer "
                f"(layer geometryType is {geom_type or 'unknown'!r})"
            )

        query_url = f"{service_url}/query"
        params = {
            "where": where_clause,
            "outFields": out_fields,
            "geometry": f"{lon_f},{lat_f}",
            "geometryType": "esriGeometryPoint",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "resultRecordCount": min(limit, 50),
            "returnGeometry": "false",
            "f": "json",
        }

        try:
            resp = await self.client.get(query_url, params=params)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Feature Service spatial query error "
                f"(HTTP {e.response.status_code}): {e.response.text}"
            ) from e

        try:
            data = resp.json()
        except Exception as json_err:
            content_type = resp.headers.get("content-type", "")
            raise ValueError(
                f"Feature Service returned non-JSON response "
                f"(content-type: {content_type})."
            ) from json_err

        error_in_body = data.get("error")
        if error_in_body:
            code = error_in_body.get("code", "unknown")
            msg = error_in_body.get("message", "Unknown error")
            details = error_in_body.get("details", [])
            detail_str = "; ".join(details) if details else ""
            raise RuntimeError(
                f"Feature Service spatial query failed (code {code}): "
                f"{msg}" + (f" — {detail_str}" if detail_str else "")
            )

        features = data.get("features", [])
        return [f.get("attributes", {}) for f in features]

    _SPATIAL_REL_MAP = {
        "intersects": "esriSpatialRelIntersects",
        "contains": "esriSpatialRelContains",
        "within": "esriSpatialRelWithin",
        "crosses": "esriSpatialRelCrosses",
        "touches": "esriSpatialRelTouches",
        "overlaps": "esriSpatialRelOverlaps",
        "envelope_intersects": "esriSpatialRelEnvelopeIntersects",
    }

    async def spatial_query_polygon(
        self,
        resource_id: str,
        filter_geometry: Optional[Dict[str, Any]] = None,
        filter_item_id: Optional[str] = None,
        filter_where: str = "1=1",
        spatial_rel: str = "intersects",
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 50,
        return_geometry: bool = False,
    ) -> List[Dict[str, Any]]:
        """Find features in a target layer that spatially relate to a polygon filter.

        The filter polygon is supplied either inline as GeoJSON via
        ``filter_geometry`` (Polygon, MultiPolygon, or Feature), or by
        dereferencing polygon features in another layer via
        ``filter_item_id`` + ``filter_where``. The target layer may be
        polygon, polyline, or point.

        Unlike a centroid-based assignment, this returns every target
        feature whose geometry touches the filter polygon, so features
        that straddle the filter boundary are still included. Set
        ``return_geometry=True`` to get GeoJSON geometries back for
        precise client-side clipping.
        """
        if limit < 1:
            raise ValueError(f"limit must be at least 1 (got {limit})")

        if not filter_geometry and not filter_item_id:
            raise ValueError(
                "spatial_query_polygon requires either filter_geometry "
                "(inline GeoJSON) or filter_item_id + filter_where"
            )

        spatial_rel_esri = self._SPATIAL_REL_MAP.get(
            (spatial_rel or "intersects").lower()
        )
        if not spatial_rel_esri:
            raise ValueError(
                f"spatial_rel must be one of "
                f"{sorted(self._SPATIAL_REL_MAP)} (got {spatial_rel!r})"
            )

        if filter_geometry:
            esri_filter = self._geojson_to_esri_polygon(filter_geometry)
        else:
            esri_filter = await self._fetch_filter_polygon(
                filter_item_id, filter_where
            )

        item = await self.get_dataset(resource_id)
        target_url = item.get("url", "")
        item_type = item.get("type", "")
        if not target_url:
            raise ValueError(
                f"Item {resource_id} does not have a queryable service URL"
            )
        if item_type and item_type not in self.QUERYABLE_TYPES:
            raise ValueError(
                f"Item type '{item_type}' is not queryable. "
                f"spatial_query_polygon supports: "
                f"{', '.join(sorted(self.QUERYABLE_TYPES))}."
            )

        target_url = self._ensure_layer_url(target_url)
        self._validate_service_url(target_url)

        where_clause = WhereValidator.validate(
            (filters or {}).get("where", "1=1")
        )
        out_fields = OutFieldsValidator.validate(
            (filters or {}).get("out_fields", "*")
        )

        max_records = (
            self.GEOMETRY_LIMIT_CAP if return_geometry else 1000
        )
        effective_limit = min(limit, max_records)

        params: Dict[str, Any] = {
            "where": where_clause,
            "outFields": out_fields,
            "geometry": json.dumps(esri_filter, separators=(",", ":")),
            "geometryType": "esriGeometryPolygon",
            "inSR": "4326",
            "spatialRel": spatial_rel_esri,
            "resultRecordCount": str(effective_limit),
            "f": "geojson" if return_geometry else "json",
            "returnGeometry": "true" if return_geometry else "false",
        }
        if return_geometry:
            params["outSR"] = "4326"
            params["maxAllowableOffset"] = str(
                self.GEOMETRY_SIMPLIFY_OFFSET_DEG
            )

        query_url = f"{target_url}/query"
        try:
            # POST because filter polygons routinely exceed URL length
            # limits — spatial_query_point can get away with GET, this
            # one cannot.
            resp = await self.client.post(query_url, data=params)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Feature Service spatial query error "
                f"(HTTP {e.response.status_code}): {e.response.text}"
            ) from e

        try:
            data = resp.json()
        except Exception as json_err:
            content_type = resp.headers.get("content-type", "")
            raise ValueError(
                f"Feature Service returned non-JSON response "
                f"(content-type: {content_type})."
            ) from json_err

        error_in_body = data.get("error")
        if error_in_body:
            code = error_in_body.get("code", "unknown")
            msg = error_in_body.get("message", "Unknown error")
            details = error_in_body.get("details", [])
            detail_str = "; ".join(details) if details else ""
            raise RuntimeError(
                f"Feature Service spatial query failed (code {code}): "
                f"{msg}" + (f" — {detail_str}" if detail_str else "")
            )

        features = data.get("features", [])
        if return_geometry:
            return [
                {
                    **(f.get("properties") or {}),
                    "__geometry__": f.get("geometry"),
                }
                for f in features
            ]
        return [f.get("attributes", {}) for f in features]

    @staticmethod
    def _geojson_to_esri_polygon(geojson: Any) -> Dict[str, Any]:
        """Convert GeoJSON Polygon / MultiPolygon / Feature to Esri polygon JSON."""
        if not isinstance(geojson, dict):
            raise ValueError(
                f"filter_geometry must be a GeoJSON object "
                f"(got {type(geojson).__name__})"
            )
        gj_type = geojson.get("type", "")
        if gj_type == "Feature":
            return AnchorageGISPlugin._geojson_to_esri_polygon(
                geojson.get("geometry") or {}
            )
        if gj_type == "Polygon":
            rings = list(geojson.get("coordinates") or [])
        elif gj_type == "MultiPolygon":
            rings = []
            for poly in geojson.get("coordinates") or []:
                rings.extend(poly)
        else:
            raise ValueError(
                f"filter_geometry must be a GeoJSON Polygon, "
                f"MultiPolygon, or Feature wrapping one "
                f"(got type={gj_type!r})"
            )
        if not rings:
            raise ValueError("filter_geometry has no polygon rings")
        return {
            "rings": rings,
            "spatialReference": {"wkid": 4326},
        }

    async def _fetch_filter_polygon(
        self, filter_item_id: str, filter_where: str
    ) -> Dict[str, Any]:
        """Resolve a polygon filter from feature(s) in another layer.

        Queries the filter layer with ``filter_where``, validates it is a
        polygon layer, and unions all matching features' rings into a
        single Esri polygon. Typical use: pick one district or one park
        feature as the filter geometry for a target layer.
        """
        filter_item_id = self._validate_item_id(filter_item_id)
        validated_where = WhereValidator.validate(filter_where or "1=1")

        item = await self.get_dataset(filter_item_id)
        url = item.get("url", "")
        if not url:
            raise ValueError(
                f"filter_item_id {filter_item_id} has no service URL"
            )
        url = self._ensure_layer_url(url)
        self._validate_service_url(url)

        meta_resp = await self.client.get(url, params={"f": "json"})
        meta_resp.raise_for_status()
        meta = meta_resp.json()
        if "error" in meta:
            raise RuntimeError(
                meta["error"].get("message", str(meta["error"]))
            )
        geom_type = meta.get("geometryType", "")
        if geom_type not in (
            "esriGeometryPolygon",
            "esriGeometryMultiPatch",
        ):
            raise ValueError(
                f"filter_item_id must point at a polygon layer "
                f"(got geometryType={geom_type or 'unknown'!r})"
            )

        query_url = f"{url}/query"
        params = {
            "where": validated_where,
            "outFields": "",
            "returnGeometry": "true",
            "outSR": "4326",
            "f": "json",
            "resultRecordCount": "50",
        }
        resp = await self.client.post(query_url, data=params)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(
                data["error"].get("message", str(data["error"]))
            )

        features = data.get("features", [])
        if not features:
            raise ValueError(
                f"filter_where {validated_where!r} matched no features "
                f"in filter layer {filter_item_id}"
            )

        all_rings: List[Any] = []
        for f in features:
            geom = f.get("geometry") or {}
            all_rings.extend(geom.get("rings") or [])

        if not all_rings:
            raise ValueError(
                f"filter features in {filter_item_id} have no "
                f"polygon rings"
            )

        return {
            "rings": all_rings,
            "spatialReference": {"wkid": 4326},
        }

    # ── Additional tool implementations ───────────────────────────────────

    async def _find_gis_content(self, args: Dict[str, Any]) -> str:
        """Combined search: gallery + spatial layers."""
        topic = args.get("topic", "").strip()
        if not topic:
            raise ValueError("topic is required")
        limit = min(int(args.get("limit", 8)), 50)

        gallery_results, layer_results = await asyncio.gather(
            self._search_gallery(topic, limit),
            self._search_org_layers(
                topic, self.LAYER_TYPES + self.DATA_TYPES, limit
            ),
        )

        city = self.plugin_config.city_name
        gallery_url = self.plugin_config.gallery_url

        if not gallery_results and not layer_results:
            return (
                f"No {city} GIS content found for topic '{topic}'.\n\n"
                f"Try different keywords, or browse the full gallery:\n"
                f"{gallery_url}"
            )

        text = f"## {city} GIS Content: '{topic}'\n\n"
        if gallery_results:
            text += (
                f"### Maps, Apps & Viewers "
                f"({len(gallery_results)} found)\n\n"
            )
            for item in gallery_results:
                text += self._format_summary(item)
        if layer_results:
            text += (
                f"### Spatial Layers & Data "
                f"({len(layer_results)} found)\n\n"
            )
            for item in layer_results:
                text += self._format_summary(item)
        text += (
            "\n---\n"
            "_Use `get_item_details` with an item ID for full description._\n"
            f"_Full gallery: {gallery_url}_"
        )
        return text

    async def _browse_gallery(self, keyword: str, limit: int) -> str:
        """Browse or search the curated gallery."""
        results = await self._search_gallery(keyword, limit)
        city = self.plugin_config.city_name
        gallery_url = self.plugin_config.gallery_url

        if not results:
            suffix = f" matching {repr(keyword)}" if keyword else ""
            return f"No gallery items found{suffix}."

        header = (
            f"## {city} GIS Gallery — '{keyword}' "
            f"({len(results)} items)\n\n"
            if keyword
            else f"## {city} GIS Gallery ({len(results)} items)\n\n"
        )
        text = header
        for item in results:
            text += self._format_summary(item)
        text += f"\n_Full gallery: {gallery_url}_"
        return text

    async def _search_spatial_layers(
        self, query: str, layer_type: str, limit: int
    ) -> str:
        """Search raw spatial layers."""
        city = self.plugin_config.city_name
        if layer_type == "layers":
            item_types = self.LAYER_TYPES
        elif layer_type == "data":
            item_types = self.DATA_TYPES
        else:
            item_types = self.LAYER_TYPES + self.DATA_TYPES

        results = await self._search_org_layers(query, item_types, limit)
        if not results:
            return f"No {city} spatial layers/data found matching '{query}'."

        text = (
            f"## {city} Spatial Layers: '{query}' "
            f"({len(results)} results)\n\n"
        )
        for item in results:
            text += self._format_summary(item)
        return text

    async def _get_layer_schema(self, args: Dict[str, Any]) -> str:
        """Fetch schema for a Feature/Map Service layer."""
        item_id = args.get("item_id", "").strip()
        service_url = args.get("service_url", "").strip()
        layer_index = int(args.get("layer_index", 0))
        keyword = args.get("keyword", "").strip().lower()
        item_title = service_url or item_id

        if item_id and not service_url:
            item_id = self._validate_item_id(item_id)
            item = await self.get_dataset(item_id)
            service_url = item.get("url", "")
            item_title = item.get("title", item_id)

        if not service_url:
            return "Error: provide either item_id or service_url."

        service_url = service_url.rstrip("/")
        if not re.search(r"/\d+$", service_url):
            service_url = f"{service_url}/{layer_index}"

        self._validate_service_url(service_url)
        resp = await self.client.get(service_url, params={"f": "json"})
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(
                data["error"].get("message", str(data["error"]))
            )

        fields = data.get("fields", [])
        layer_name = data.get("name", data.get("title", "Unknown Layer"))
        geometry_type = data.get("geometryType", "N/A")
        max_records = data.get("maxRecordCount", "Unknown")

        # Prefer the layer name over a raw URL for the heading
        if not item_id and layer_name != "Unknown Layer":
            item_title = layer_name

        if keyword:
            fields = [
                f
                for f in fields
                if keyword in f.get("name", "").lower()
                or keyword in f.get("alias", "").lower()
            ]

        if not fields:
            return (
                f"No fields matching '{keyword}' in layer '{layer_name}'."
                if keyword
                else f"No fields found in layer '{layer_name}'."
            )

        text = f"## Schema: {item_title}\n"
        text += (
            f"**Layer:** {layer_name}  |  "
            f"**Geometry:** {geometry_type}  |  "
            f"**Max Records:** {max_records}\n"
        )
        text += f"**Service URL:** {service_url}\n\n"
        if keyword:
            text += f"_(Filtered to fields matching '{keyword}')_\n\n"
        text += f"### Fields ({len(fields)})\n\n"
        text += "| Field Name | Alias | Type |\n|---|---|---|\n"
        for f in fields:
            name = f.get("name", "")
            alias = f.get("alias", name)
            ftype = f.get("type", "").replace("esriFieldType", "")
            text += f"| `{name}` | {alias} | {ftype} |\n"

        domain_lines = []
        for f in fields:
            domain = f.get("domain")
            if domain and domain.get("type") == "codedValue":
                dname = domain.get("name", f.get("name", ""))
                codes = domain.get("codedValues", [])
                if codes:
                    vals = ", ".join(
                        f"{c['code']}={c['name']}" for c in codes[:15]
                    )
                    if len(codes) > 15:
                        vals += f" ... (+{len(codes) - 15} more)"
                    domain_lines.append(f"- **{dname}**: {vals}")

        if domain_lines:
            text += "\n### Coded Domains\n" + "\n".join(domain_lines)

        return text

    async def _search_layers_by_field(self, args: Dict[str, Any]) -> str:
        """Find services containing a specific field name/alias."""
        field_keyword = args.get("field_keyword", "").strip().lower()
        if not field_keyword:
            raise ValueError("field_keyword is required")
        service_keyword = args.get("service_keyword", "").strip()
        limit = min(int(args.get("limit", 10)), 20)

        type_filter = " OR ".join(f'type:"{t}"' for t in self.LAYER_TYPES)
        clauses = [
            f"orgid:{self.plugin_config.org_id}",
            f"({type_filter})",
        ]
        # Without a text filter, ArcGIS ranks the catalog by global popularity
        # and the top-`limit` pool skips less-trafficked services. Fall back to
        # the field_keyword so the sample is at least biased toward services
        # whose titles/descriptions mention the attribute of interest.
        effective_service_filter = service_keyword or field_keyword
        clauses.append(effective_service_filter)
        candidates = await self._run_search(" AND ".join(clauses), limit)

        if not candidates:
            return (
                f"No services found matching '{effective_service_filter}'."
            )

        async def check_service(
            item: Dict[str, Any],
        ) -> List[Dict[str, Any]]:
            url = (item.get("url") or "").rstrip("/")
            if not url:
                return []
            try:
                self._validate_service_url(url)
            except ValueError:
                return []
            try:
                # Fetch service root to discover all layers
                resp = await self.client.get(
                    url, params={"f": "json"}, timeout=10.0
                )
                root = resp.json()
                layer_list = root.get("layers", [])
                if not layer_list:
                    # Fallback: single-layer service, check /0
                    layer_list = [{"id": 0}]

                hits = []
                for layer_meta in layer_list:
                    layer_id = layer_meta.get("id", 0)
                    try:
                        lr = await self.client.get(
                            f"{url}/{layer_id}",
                            params={"f": "json"},
                            timeout=10.0,
                        )
                        data = lr.json()
                        matching = [
                            f
                            for f in data.get("fields", [])
                            if field_keyword in f.get("name", "").lower()
                            or field_keyword in f.get("alias", "").lower()
                        ]
                        if matching:
                            hits.append(
                                {
                                    "item": item,
                                    "layer_name": data.get("name", ""),
                                    "layer_index": layer_id,
                                    "matching_fields": matching,
                                }
                            )
                    except Exception:
                        continue
                return hits
            except Exception:
                return []

        results_raw = await asyncio.gather(
            *[check_service(item) for item in candidates]
        )
        matches = [m for hits in results_raw for m in hits]

        city = self.plugin_config.city_name

        if not matches:
            inspected = "\n".join(
                f"- {item.get('title', 'Untitled')}" for item in candidates
            )
            filter_note = (
                f"pre-filtered by '{service_keyword}'"
                if service_keyword
                else f"auto-filtered by field_keyword '{field_keyword}'"
            )
            return (
                f"None of the {len(candidates)} {city} services inspected "
                f"contain fields matching '{field_keyword}' ({filter_note}).\n\n"
                f"Inspected services:\n{inspected}\n\n"
                f"Try a different `service_keyword` to broaden the search."
            )

        text = (
            f"## {city} Layers with '{field_keyword}' Fields\n\n"
            f"Found {len(matches)} layer(s) with matching fields "
            f"(checked {len(candidates)} services):\n\n"
        )
        for m in matches:
            item = m["item"]
            title = item.get("title", "Untitled")
            item_id = item.get("id", "")
            layer_idx = m.get("layer_index", 0)
            layer_suffix = (
                f" (layer {layer_idx})" if layer_idx != 0 else ""
            )
            text += f"### {title}{layer_suffix}\n"
            text += (
                f"**Layer:** {m['layer_name']}  |  **ID:** `{item_id}`\n"
            )
            text += "**Matching fields:**\n"
            for f in m["matching_fields"]:
                name = f.get("name", "")
                alias = f.get("alias", "")
                ftype = f.get("type", "").replace("esriFieldType", "")
                label = f"`{name}`" + (
                    f" ({alias})" if alias != name else ""
                )
                text += f"- {label} — {ftype}\n"
            text += (
                f"**Portal:** "
                f"{self._portal_home}/home/item.html?id={item_id}\n\n"
            )
        text += (
            "_Use `get_layer_schema` with an item_id to see the "
            "complete field list._"
        )
        return text

    # ── Static helpers ────────────────────────────────────────────────────

    @classmethod
    def _validate_service_url(cls, url: str) -> str:
        """Reject any URL whose host is not on the allowlist.

        Prevents SSRF via user-supplied service URLs and item URLs returned
        from portal search results.
        """
        if not url:
            raise ValueError("service URL cannot be empty")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"service URL must use http or https (got {parsed.scheme!r})"
            )
        host = (parsed.hostname or "").lower()
        if not host:
            raise ValueError("service URL must include a hostname")
        if not any(
            host == suffix.lstrip(".") or host.endswith(suffix)
            for suffix in cls.ALLOWED_HOST_SUFFIXES
        ):
            raise ValueError(
                f"service URL host {host!r} is not on the allowlist "
                f"({', '.join(cls.ALLOWED_HOST_SUFFIXES)})"
            )
        return url

    @classmethod
    def _validate_item_id(cls, item_id: str) -> str:
        """Require an ArcGIS item ID to be a 32-char hex string."""
        if not item_id or not cls.ITEM_ID_RE.match(item_id):
            raise ValueError(
                f"Invalid ArcGIS item id: {item_id!r} "
                f"(expected 32-character hex string)"
            )
        return item_id.lower()

    @staticmethod
    def _ensure_layer_url(service_url: str) -> str:
        """Append /0 if URL points at a FeatureServer/MapServer root."""
        stripped = service_url.rstrip("/")
        if re.search(r"/(FeatureServer|MapServer)$", stripped, re.IGNORECASE):
            return f"{stripped}/0"
        return stripped

    @staticmethod
    def _validate_lonlat(lon: Any, lat: Any) -> tuple[float, float]:
        """Validate WGS84 coordinates. Note: lon first, then lat."""
        try:
            lon_f = float(lon)
            lat_f = float(lat)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"lon/lat must be numeric (got lon={lon!r}, lat={lat!r})"
            ) from e
        if not (-180.0 <= lon_f <= 180.0):
            raise ValueError(
                f"lon out of range [-180, 180]: {lon_f}"
            )
        if not (-90.0 <= lat_f <= 90.0):
            raise ValueError(
                f"lat out of range [-90, 90]: {lat_f}"
            )
        return lon_f, lat_f

    async def _get_record_count(
        self, service_url: str, where: str
    ) -> Optional[int]:
        """Fetch total record count for a query (best-effort)."""
        try:
            self._validate_service_url(service_url)
        except ValueError:
            return None
        query_url = f"{service_url}/query"
        try:
            resp = await self.client.get(
                query_url,
                params={
                    "where": where,
                    "returnCountOnly": "true",
                    "f": "json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            count = data.get("count")
            return int(count) if count is not None else None
        except Exception:
            return None

    async def _get_date_fields(self, service_url: str) -> Optional[set]:
        """Fetch field names with esriFieldTypeDate type (best-effort)."""
        try:
            self._validate_service_url(service_url)
        except ValueError:
            return None
        try:
            resp = await self.client.get(
                service_url, params={"f": "json"}
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                f["name"]
                for f in data.get("fields", [])
                if f.get("type") == "esriFieldTypeDate"
            } or None
        except Exception:
            return None

    # ── Aggregation helpers ───────────────────────────────────────────────

    # Cache TTL for aggregation layers (councils, districts, etc.). Boundary
    # layers change rarely; paying 24h of staleness buys a huge hit-rate win
    # on repeat analyses.
    AGG_CACHE_TTL_SECONDS = 86400

    # Cap on LRU entries. Each entry can hold up to AGG_SOURCE_LIMIT polygons;
    # this bounds worst-case memory at roughly 32 * ~5 MB = ~160 MB and
    # defangs WHERE-clause-variant cache-busting DoS attempts.
    AGG_CACHE_MAX_ENTRIES = 32

    # Safety cap on source features pulled for a single aggregation call.
    # A council-by-council rollup of a city-wide dataset is typically a few
    # hundred to a few thousand features; beyond this the analysis is
    # probably better served by a server-side stats endpoint.
    AGG_SOURCE_LIMIT = 5000

    # ArcGIS maxRecordCount is usually 1000 or 2000. We page through with
    # resultOffset at this step until AGG_SOURCE_LIMIT is reached.
    AGG_PAGE_SIZE = 1000

    @staticmethod
    def _ring_contains_point(
        ring: List[List[float]], point: Tuple[float, float]
    ) -> bool:
        """Even-odd ray-cast point-in-ring test (2D, lon/lat).

        Expects ring as a list of [lon, lat] pairs (GeoJSON style, first
        and last point equal). Returns True if the point is strictly
        inside the ring; boundary behavior is not guaranteed (doesn't
        matter for aggregation — a point exactly on a council boundary
        falls into exactly one bucket under first_match).
        """
        x, y = point
        inside = False
        n = len(ring)
        if n < 3:
            return False
        j = n - 1
        for i in range(n):
            xi, yi = ring[i][0], ring[i][1]
            xj, yj = ring[j][0], ring[j][1]
            # Does the horizontal ray at y cross edge (i, j)?
            intersect = ((yi > y) != (yj > y)) and (
                x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-300) + xi
            )
            if intersect:
                inside = not inside
            j = i
        return inside

    @classmethod
    def _polygon_contains_point(
        cls,
        polygon: List[List[List[float]]],
        point: Tuple[float, float],
    ) -> bool:
        """Point-in-polygon for a GeoJSON Polygon coordinates array.

        Rings after the first are treated as holes: a point inside a hole
        is not inside the polygon.
        """
        if not polygon:
            return False
        if not cls._ring_contains_point(polygon[0], point):
            return False
        for hole in polygon[1:]:
            if cls._ring_contains_point(hole, point):
                return False
        return True

    @classmethod
    def _multipolygon_contains_point(
        cls,
        multipolygon: List[List[List[List[float]]]],
        point: Tuple[float, float],
    ) -> bool:
        return any(cls._polygon_contains_point(p, point) for p in multipolygon)

    @classmethod
    def _geometry_contains_point(
        cls, geometry: Dict[str, Any], point: Tuple[float, float]
    ) -> bool:
        gtype = (geometry or {}).get("type", "")
        coords = (geometry or {}).get("coordinates")
        if not coords:
            return False
        if gtype == "Polygon":
            return cls._polygon_contains_point(coords, point)
        if gtype == "MultiPolygon":
            return cls._multipolygon_contains_point(coords, point)
        return False

    @staticmethod
    def _ring_area(ring: List[List[float]]) -> float:
        """Shoelace area (signed). Positive for CCW rings."""
        area = 0.0
        n = len(ring)
        if n < 3:
            return 0.0
        for i in range(n):
            x1, y1 = ring[i][0], ring[i][1]
            x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
            area += x1 * y2 - x2 * y1
        return area / 2.0

    @classmethod
    def _polygon_area(cls, polygon: List[List[List[float]]]) -> float:
        if not polygon:
            return 0.0
        a = abs(cls._ring_area(polygon[0]))
        for hole in polygon[1:]:
            a -= abs(cls._ring_area(hole))
        return a

    @classmethod
    def _geometry_area(cls, geometry: Dict[str, Any]) -> float:
        gtype = (geometry or {}).get("type", "")
        coords = (geometry or {}).get("coordinates") or []
        if gtype == "Polygon":
            return cls._polygon_area(coords)
        if gtype == "MultiPolygon":
            return sum(cls._polygon_area(p) for p in coords)
        return 0.0

    @staticmethod
    def _ring_centroid(ring: List[List[float]]) -> Tuple[float, float]:
        """Area-weighted centroid of a ring (first==last tolerated).

        Falls back to arithmetic mean of vertices for degenerate rings
        (colinear points, zero area).
        """
        n = len(ring)
        if n == 0:
            return (0.0, 0.0)
        # Drop duplicated closing vertex if present
        if n > 1 and ring[0] == ring[-1]:
            pts = ring[:-1]
        else:
            pts = ring
        m = len(pts)
        if m < 3:
            sx = sum(p[0] for p in pts) / m
            sy = sum(p[1] for p in pts) / m
            return (sx, sy)
        cx = 0.0
        cy = 0.0
        a = 0.0
        for i in range(m):
            x1, y1 = pts[i][0], pts[i][1]
            x2, y2 = pts[(i + 1) % m][0], pts[(i + 1) % m][1]
            cross = x1 * y2 - x2 * y1
            a += cross
            cx += (x1 + x2) * cross
            cy += (y1 + y2) * cross
        a *= 0.5
        if abs(a) < 1e-18:
            sx = sum(p[0] for p in pts) / m
            sy = sum(p[1] for p in pts) / m
            return (sx, sy)
        return (cx / (6.0 * a), cy / (6.0 * a))

    @classmethod
    def _geometry_centroid(
        cls, geometry: Dict[str, Any]
    ) -> Optional[Tuple[float, float]]:
        """Centroid of a GeoJSON geometry (Polygon/MultiPolygon/Point).

        For MultiPolygon, returns the area-weighted centroid of the
        constituent polygon centroids.
        """
        gtype = (geometry or {}).get("type", "")
        coords = (geometry or {}).get("coordinates")
        if coords is None:
            return None
        if gtype == "Point":
            return (float(coords[0]), float(coords[1]))
        if gtype == "Polygon":
            if not coords:
                return None
            return cls._ring_centroid(coords[0])
        if gtype == "MultiPolygon":
            if not coords:
                return None
            sum_x = 0.0
            sum_y = 0.0
            sum_a = 0.0
            for poly in coords:
                if not poly:
                    continue
                area = cls._polygon_area(poly)
                cx, cy = cls._ring_centroid(poly[0])
                sum_x += cx * area
                sum_y += cy * area
                sum_a += area
            if sum_a <= 0:
                # Fall back to centroid of largest polygon
                largest = max(coords, key=lambda p: cls._polygon_area(p))
                return cls._ring_centroid(largest[0]) if largest else None
            return (sum_x / sum_a, sum_y / sum_a)
        return None

    @classmethod
    def _geometry_representative_point(
        cls, geometry: Dict[str, Any]
    ) -> Optional[Tuple[float, float]]:
        """Return a point guaranteed to be inside the geometry (best-effort).

        Uses the centroid when it's inside the geometry, otherwise sweeps
        a horizontal line at the centroid's y through the polygon and
        returns the midpoint of the widest interior segment. Final fallback
        is the first outer-ring vertex.
        """
        gtype = (geometry or {}).get("type", "")
        if gtype == "Point":
            coords = geometry.get("coordinates") or []
            return (float(coords[0]), float(coords[1])) if coords else None

        centroid = cls._geometry_centroid(geometry)
        if centroid is None:
            return None
        if cls._geometry_contains_point(geometry, centroid):
            return centroid

        # Horizontal sweep at y = centroid.y to find an interior segment.
        y = centroid[1]
        rings: List[List[List[float]]] = []
        coords = geometry.get("coordinates") or []
        if gtype == "Polygon":
            rings = list(coords)
        elif gtype == "MultiPolygon":
            for poly in coords:
                rings.extend(poly)

        xs: List[float] = []
        for ring in rings:
            n = len(ring)
            if n < 2:
                continue
            for i in range(n):
                x1, y1 = ring[i][0], ring[i][1]
                x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
                if (y1 > y) == (y2 > y):
                    continue
                dy = y2 - y1
                if dy == 0:
                    continue
                t = (y - y1) / dy
                xs.append(x1 + t * (x2 - x1))
        xs.sort()
        # Take the widest interior span (odd pairs are inside under even-odd).
        best_mid = None
        best_width = -1.0
        for i in range(0, len(xs) - 1, 2):
            mid = (xs[i] + xs[i + 1]) / 2.0
            width = xs[i + 1] - xs[i]
            if width > best_width and cls._geometry_contains_point(
                geometry, (mid, y)
            ):
                best_width = width
                best_mid = (mid, y)
        if best_mid is not None:
            return best_mid

        # Last resort: first outer-ring vertex.
        if gtype == "Polygon" and coords and coords[0]:
            v = coords[0][0]
            return (float(v[0]), float(v[1]))
        if gtype == "MultiPolygon" and coords and coords[0] and coords[0][0]:
            v = coords[0][0][0]
            return (float(v[0]), float(v[1]))
        return centroid

    @classmethod
    def _feature_to_point(
        cls, geometry: Dict[str, Any], centroid_mode: str
    ) -> Optional[Tuple[float, float]]:
        """Reduce a feature's geometry to a single (lon, lat) point."""
        if not geometry:
            return None
        gtype = geometry.get("type", "")
        if gtype == "Point":
            coords = geometry.get("coordinates") or []
            return (float(coords[0]), float(coords[1])) if coords else None
        if gtype == "MultiPoint":
            coords = geometry.get("coordinates") or []
            return (float(coords[0][0]), float(coords[0][1])) if coords else None
        if centroid_mode == "centroid":
            return cls._geometry_centroid(geometry)
        if centroid_mode == "representative_point":
            return cls._geometry_representative_point(geometry)
        # auto: centroid if inside, else representative_point
        centroid = cls._geometry_centroid(geometry)
        if centroid is not None and cls._geometry_contains_point(
            geometry, centroid
        ):
            return centroid
        return cls._geometry_representative_point(geometry)

    async def _resolve_layer_url(self, item_id: str) -> str:
        """Resolve an item ID to a validated /FeatureServer/N query URL."""
        item_id = self._validate_item_id(item_id)
        item = await self.get_dataset(item_id)
        url = item.get("url", "")
        item_type = item.get("type", "")
        if not url:
            raise ValueError(
                f"Item {item_id} has no queryable service URL"
            )
        if item_type and item_type not in self.QUERYABLE_TYPES:
            raise ValueError(
                f"Item type '{item_type}' is not queryable."
            )
        url = self._ensure_layer_url(url)
        self._validate_service_url(url)
        return url

    async def _fetch_layer_meta(self, layer_url: str) -> Dict[str, Any]:
        resp = await self.client.get(layer_url, params={"f": "json"})
        resp.raise_for_status()
        meta = resp.json()
        if "error" in meta:
            raise RuntimeError(
                meta["error"].get("message", str(meta["error"]))
            )
        return meta

    async def _fetch_aggregation_polygons(
        self,
        aggregation_item_id: str,
        group_by_field: str,
        agg_where: str,
    ) -> List[Dict[str, Any]]:
        """Fetch polygons from an aggregation layer with their group value.

        Returns [{'group': value, 'geometry': {..GeoJSON..}, 'objectid': N}].
        Cached by (item_id, group_by_field, agg_where) for AGG_CACHE_TTL_SECONDS.
        """
        validated_where = WhereValidator.validate(agg_where or "1=1")
        cache_key = (
            self._validate_item_id(aggregation_item_id),
            group_by_field,
            validated_where,
        )
        now = time.time()
        cached = self._agg_layer_cache.get(cache_key)
        if cached and cached[0] > now:
            self._agg_layer_cache.move_to_end(cache_key)
            return cached[1]
        if cached:
            # Expired — drop and refetch.
            del self._agg_layer_cache[cache_key]

        layer_url = await self._resolve_layer_url(aggregation_item_id)
        meta = await self._fetch_layer_meta(layer_url)
        geom_type = meta.get("geometryType", "")
        if geom_type not in (
            "esriGeometryPolygon",
            "esriGeometryMultiPatch",
        ):
            raise ValueError(
                f"aggregation_item_id must point at a polygon layer "
                f"(got geometryType={geom_type or 'unknown'!r})"
            )
        field_names = {f.get("name") for f in meta.get("fields", [])}
        if group_by_field not in field_names:
            raise ValueError(
                f"group_by_field {group_by_field!r} is not a field on "
                f"the aggregation layer. Available fields: "
                f"{sorted(field_names)[:12]}..."
            )

        polygons = await self._paged_geojson_fetch(
            layer_url,
            where=validated_where,
            out_fields=group_by_field,
            limit=self.AGG_SOURCE_LIMIT,
        )
        result = [
            {
                "group": (f.get("properties") or {}).get(group_by_field),
                "geometry": f.get("geometry"),
            }
            for f in polygons
            if f.get("geometry")
        ]
        self._agg_layer_cache[cache_key] = (
            now + self.AGG_CACHE_TTL_SECONDS,
            result,
        )
        # LRU eviction to bound memory under cache-busting inputs.
        while len(self._agg_layer_cache) > self.AGG_CACHE_MAX_ENTRIES:
            self._agg_layer_cache.popitem(last=False)
        return result

    async def _paged_geojson_fetch(
        self,
        layer_url: str,
        where: str,
        out_fields: str,
        limit: int,
    ) -> List[Dict[str, Any]]:
        """Page through a layer and return raw GeoJSON features (geom + props)."""
        query_url = f"{layer_url}/query"
        out_fields = OutFieldsValidator.validate(out_fields or "*")
        features: List[Dict[str, Any]] = []
        offset = 0
        page_size = self.AGG_PAGE_SIZE
        while len(features) < limit:
            params = {
                "where": where,
                "outFields": out_fields,
                "returnGeometry": "true",
                "outSR": "4326",
                "f": "geojson",
                "resultRecordCount": str(min(page_size, limit - len(features))),
                "resultOffset": str(offset),
                "maxAllowableOffset": str(self.GEOMETRY_SIMPLIFY_OFFSET_DEG),
            }
            resp = await self.client.get(query_url, params=params)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise RuntimeError(
                    data["error"].get("message", str(data["error"]))
                )
            page = data.get("features") or []
            if not page:
                break
            features.extend(page)
            offset += len(page)
            # If we got less than requested, we're done.
            if len(page) < page_size:
                break
        return features

    async def _aggregate_by_polygon(self, args: Dict[str, Any]) -> str:
        source_item_id = self._validate_item_id(
            (args.get("source_item_id") or "").strip()
        )
        aggregation_item_id = self._validate_item_id(
            (args.get("aggregation_item_id") or "").strip()
        )
        group_by_field = (args.get("group_by_field") or "").strip()
        if not group_by_field:
            raise ValueError("group_by_field is required")
        raw_sum_fields = args.get("sum_fields") or []
        if isinstance(raw_sum_fields, str):
            raw_sum_fields = [
                s.strip() for s in raw_sum_fields.split(",") if s.strip()
            ]
        sum_fields: List[str] = list(raw_sum_fields)
        include_count = bool(args.get("count", True))
        # Validate both WHERE clauses up front so malformed/injection
        # attempts are rejected before any upstream I/O (otherwise a bad
        # source_where would still pay the aggregation-layer fetch cost).
        validated_source_where = WhereValidator.validate(
            args.get("source_where") or "1=1"
        )
        agg_where = WhereValidator.validate(args.get("agg_where") or "1=1")
        centroid_mode = (args.get("centroid_mode") or "auto").lower()
        if centroid_mode not in ("auto", "centroid", "representative_point"):
            raise ValueError(
                "centroid_mode must be one of: auto, centroid, "
                "representative_point"
            )
        overlap_policy = (args.get("overlap_policy") or "first_match").lower()
        if overlap_policy not in ("first_match", "all_matches", "largest"):
            raise ValueError(
                "overlap_policy must be one of: first_match, "
                "all_matches, largest"
            )
        max_source = min(
            int(args.get("max_source_features", self.AGG_SOURCE_LIMIT)),
            self.AGG_SOURCE_LIMIT,
        )

        # Fetch aggregation polygons first so we can validate the group field
        # name before paying for a source-layer fetch.
        agg_polygons = await self._fetch_aggregation_polygons(
            aggregation_item_id, group_by_field, agg_where
        )
        if not agg_polygons:
            raise ValueError(
                f"agg_where {agg_where!r} matched no polygons on "
                f"the aggregation layer"
            )

        # Fetch source features. Validate sum_fields exist.
        source_url = await self._resolve_layer_url(source_item_id)
        source_meta = await self._fetch_layer_meta(source_url)
        source_fields = {f.get("name") for f in source_meta.get("fields", [])}
        numeric_types = {
            "esriFieldTypeInteger",
            "esriFieldTypeSmallInteger",
            "esriFieldTypeDouble",
            "esriFieldTypeSingle",
            "esriFieldTypeOID",
        }
        numeric_fields = {
            f.get("name")
            for f in source_meta.get("fields", [])
            if f.get("type") in numeric_types
        }
        for f in sum_fields:
            if f not in source_fields:
                raise ValueError(
                    f"sum_fields entry {f!r} is not a field on the "
                    f"source layer"
                )
            if f not in numeric_fields:
                raise ValueError(
                    f"sum_fields entry {f!r} is not a numeric field "
                    f"(cannot be summed)"
                )
        source_geom_type = source_meta.get("geometryType", "")

        # Request only the fields we need + geometry.
        out_fields = ",".join(sum_fields) if sum_fields else "OBJECTID"
        source_features = await self._paged_geojson_fetch(
            source_url,
            where=validated_source_where,
            out_fields=out_fields,
            limit=max_source,
        )

        # Reduce each source feature to a point.
        source_points: List[Tuple[Tuple[float, float], Dict[str, Any]]] = []
        for feat in source_features:
            props = feat.get("properties") or {}
            geom = feat.get("geometry") or {}
            point = self._feature_to_point(geom, centroid_mode)
            if point is None:
                continue
            source_points.append((point, props))

        # Pre-compute polygon areas for 'largest' policy.
        if overlap_policy == "largest":
            for p in agg_polygons:
                p["_area"] = self._geometry_area(p["geometry"])

        buckets: Dict[Any, Dict[str, Any]] = defaultdict(
            lambda: {"count": 0, **{f: 0.0 for f in sum_fields}}
        )
        unmatched_count = 0
        for point, props in source_points:
            matches = [
                p for p in agg_polygons
                if self._geometry_contains_point(p["geometry"], point)
            ]
            if not matches:
                unmatched_count += 1
                continue
            if overlap_policy == "first_match":
                matches = matches[:1]
            elif overlap_policy == "largest":
                matches = [max(matches, key=lambda p: p.get("_area", 0.0))]
            for p in matches:
                b = buckets[p["group"]]
                b["count"] += 1
                for fld in sum_fields:
                    v = props.get(fld)
                    if v is None:
                        continue
                    try:
                        b[fld] = (b[fld] or 0) + float(v)
                    except (TypeError, ValueError):
                        continue

        # Format: sorted by count desc for readability.
        bucket_list = sorted(
            buckets.items(),
            key=lambda kv: kv[1]["count"],
            reverse=True,
        )

        city = self.plugin_config.city_name
        lines = [
            f"## Aggregation: {source_item_id} → {aggregation_item_id}",
            f"**City:** {city}  |  **Group field:** `{group_by_field}`",
            f"**Source geometry:** {source_geom_type}  |  "
            f"**Centroid mode:** {centroid_mode}  |  "
            f"**Overlap policy:** {overlap_policy}",
            f"**Source features:** {len(source_points):,}  |  "
            f"**Buckets:** {len(bucket_list)}  |  "
            f"**Unmatched:** {unmatched_count:,}",
            "",
        ]
        if not bucket_list:
            lines.append(
                "_No source features fell inside any aggregation polygon._"
            )
            return "\n".join(lines)

        header_cols = ["Group"]
        if include_count:
            header_cols.append("Count")
        header_cols.extend(sum_fields)
        lines.append("| " + " | ".join(header_cols) + " |")
        lines.append("|" + "---|" * len(header_cols))
        for group, bucket in bucket_list:
            row = [str(group)]
            if include_count:
                row.append(f"{bucket['count']:,}")
            for fld in sum_fields:
                val = bucket[fld]
                if isinstance(val, float) and val.is_integer():
                    row.append(f"{int(val):,}")
                else:
                    row.append(f"{val:,}")
            lines.append("| " + " | ".join(row) + " |")

        if unmatched_count:
            lines += [
                "",
                f"_{unmatched_count:,} source feature(s) fell outside "
                f"every aggregation polygon. This usually indicates "
                f"data-quality signal (stray coordinates, records "
                f"outside the city boundary)._",
            ]
        if len(source_features) >= max_source:
            lines += [
                "",
                f"_Source fetch hit the {max_source:,}-feature cap. "
                f"Narrow source_where to get a complete picture._",
            ]
        return "\n".join(lines)

    async def _filter_by_polygon(self, args: Dict[str, Any]) -> str:
        source_item_id = self._validate_item_id(
            (args.get("source_item_id") or "").strip()
        )
        container_item_id = self._validate_item_id(
            (args.get("container_item_id") or "").strip()
        )
        container_where = (args.get("container_where") or "").strip()
        if not container_where:
            raise ValueError(
                "container_where is required — it identifies which "
                "polygon(s) in the container layer to filter against"
            )
        validated_container_where = WhereValidator.validate(container_where)
        # Validate source_where up front too — rejection shouldn't wait
        # for the container-layer lookup to complete.
        source_where = WhereValidator.validate(
            args.get("source_where") or "1=1"
        )
        out_fields = args.get("out_fields", "*")
        return_geometry = bool(args.get("return_geometry", False))
        requested_limit = int(args.get("limit", 100))
        effective_limit = (
            min(requested_limit, self.GEOMETRY_LIMIT_CAP)
            if return_geometry
            else min(requested_limit, 1000)
        )

        # Resolve the container polygon(s) up-front so we can report a
        # friendly 0-match error instead of silently returning no records.
        container_url = await self._resolve_layer_url(container_item_id)
        container_meta = await self._fetch_layer_meta(container_url)
        geom_type = container_meta.get("geometryType", "")
        if geom_type not in (
            "esriGeometryPolygon",
            "esriGeometryMultiPatch",
        ):
            raise ValueError(
                f"container_item_id must point at a polygon layer "
                f"(got geometryType={geom_type or 'unknown'!r})"
            )

        count_resp = await self.client.get(
            f"{container_url}/query",
            params={
                "where": validated_container_where,
                "returnCountOnly": "true",
                "f": "json",
            },
        )
        count_resp.raise_for_status()
        count_data = count_resp.json()
        if "error" in count_data:
            raise RuntimeError(
                count_data["error"].get(
                    "message", str(count_data["error"])
                )
            )
        matched_polygons = int(count_data.get("count") or 0)
        if matched_polygons == 0:
            return (
                f"Error: container_where {container_where!r} matched 0 "
                f"polygons on container layer `{container_item_id}`. "
                f"Did you misspell a name? Try "
                f"`search_spatial_layers` or `query_data` to browse "
                f"valid values for the container field."
            )

        # Delegate the actual spatial query to spatial_query_polygon's
        # filter-item pathway so we get server-side intersection and
        # consistent result formatting with the rest of the plugin.
        records = await self.spatial_query_polygon(
            source_item_id,
            filter_geometry=None,
            filter_item_id=container_item_id,
            filter_where=validated_container_where,
            spatial_rel="intersects",
            filters={
                "where": source_where,
                "out_fields": out_fields,
            },
            limit=effective_limit,
            return_geometry=return_geometry,
        )

        city = self.plugin_config.city_name
        header = (
            f"## Filter: {source_item_id} inside "
            f"{container_item_id} where {container_where!r}\n"
            f"**City:** {city}  |  "
            f"**Container polygons matched:** {matched_polygons:,}\n\n"
        )
        if not records:
            return (
                header
                + f"No features in `{source_item_id}` fall inside the "
                f"selected polygon(s)."
            )
        body = self._format_query_results(
            records, effective_limit, total_count=None, date_fields=None
        )
        return header + body

    # ── Tool definitions ──────────────────────────────────────────────────

    def get_tools(self) -> List[ToolDefinition]:
        city = (
            self.plugin_config.city_name if self.plugin_config else "Unknown"
        )
        return [
            ToolDefinition(
                name="find_gis_content",
                description=(
                    f"Search {city}'s GIS portal for maps, apps, and datasets "
                    f"related to a topic. Use when someone asks 'do you have "
                    f"data about X?' — e.g. 'flood zones', 'trails', 'zoning', "
                    f"'schools', 'crime', 'avalanche', 'parks'. Searches both "
                    f"the curated public gallery AND the organization's raw "
                    f"spatial layers."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "topic": {
                            "type": "string",
                            "description": (
                                "Topic to search for, e.g. 'flood zones', "
                                "'trails', 'zoning'."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results per source (default 8).",
                            "default": 8,
                        },
                    },
                    "required": ["topic"],
                },
            ),
            ToolDefinition(
                name="browse_gallery",
                description=(
                    f"Browse or search {city}'s curated public GIS gallery — "
                    f"interactive maps, dashboards, apps, and StoryMaps. "
                    f"Optionally filter by keyword."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "keyword": {
                            "type": "string",
                            "description": "Optional keyword to filter gallery items.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results (default 20).",
                            "default": 20,
                        },
                    },
                },
            ),
            ToolDefinition(
                name="search_spatial_layers",
                description=(
                    f"Search {city}'s ArcGIS Online for raw spatial layers — "
                    f"Feature Services, Map Services, tile layers, Web Maps, "
                    f"and downloadable data (GeoJSON, Shapefile, CSV). Use "
                    f"when the user wants underlying GIS data rather than a "
                    f"pre-built viewer."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Keyword search query.",
                        },
                        "layer_type": {
                            "type": "string",
                            "enum": ["layers", "data", "all"],
                            "description": (
                                "'layers' = Feature/Map/Image Services and "
                                "tile layers; 'data' = Web Maps, GeoJSON, "
                                "Shapefile, CSV; 'all' = both."
                            ),
                            "default": "all",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results (default 10, max 50).",
                            "default": 10,
                        },
                    },
                    "required": ["query"],
                },
            ),
            ToolDefinition(
                name="get_item_details",
                description=(
                    f"Get full details for a specific {city} GIS item by its "
                    f"ArcGIS item ID: title, full description, tags, owner, "
                    f"dates, service URL, and spatial extent."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": (
                                "ArcGIS Online item ID "
                                "(32-character hex string)."
                            ),
                        },
                    },
                    "required": ["item_id"],
                },
            ),
            ToolDefinition(
                name="get_layer_schema",
                description=(
                    f"Fetch the schema (field names, types, aliases, coded "
                    f"domains) for a Feature Service or Map Service layer. "
                    f"Use to discover what attributes a dataset contains. "
                    f"Accepts an ArcGIS item ID or a direct service URL."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": (
                                "ArcGIS Online item ID. "
                                "Use this or service_url."
                            ),
                        },
                        "service_url": {
                            "type": "string",
                            "description": (
                                "Direct ArcGIS REST service URL ending in "
                                "/FeatureServer or /FeatureServer/0."
                            ),
                        },
                        "layer_index": {
                            "type": "integer",
                            "description": (
                                "Layer index within the service (default 0)."
                            ),
                            "default": 0,
                        },
                        "keyword": {
                            "type": "string",
                            "description": (
                                "Only show fields whose name/alias "
                                "contains this term."
                            ),
                        },
                    },
                },
            ),
            ToolDefinition(
                name="search_layers_by_field",
                description=(
                    f"Find {city} Feature Services that contain a specific "
                    f"field name or alias. Use to discover which datasets "
                    f"have a particular attribute — e.g. 'flood', 'permit', "
                    f"'zone'."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "field_keyword": {
                            "type": "string",
                            "description": (
                                "Keyword to match in field names/aliases."
                            ),
                        },
                        "service_keyword": {
                            "type": "string",
                            "description": (
                                "Optional: pre-filter services by title "
                                "keyword. If omitted, `field_keyword` is "
                                "used as the catalog filter so the inspected "
                                "sample is biased toward relevant services "
                                "(the ArcGIS catalog is popularity-ranked, "
                                "so an unfiltered search often misses "
                                "less-trafficked services)."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                "Max services to inspect "
                                "(default 10, max 20)."
                            ),
                            "default": 10,
                        },
                    },
                    "required": ["field_keyword"],
                },
            ),
            ToolDefinition(
                name="query_data",
                description=(
                    f"Query records from a {city} ArcGIS Feature Service. "
                    f"Provide the item ID — the plugin resolves the service "
                    f"URL automatically. Use get_item_details first to confirm "
                    f"the item has a queryable service URL."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": "ArcGIS Online item ID.",
                        },
                        "where": {
                            "type": "string",
                            "description": "SQL WHERE clause for filtering.",
                            "default": "1=1",
                        },
                        "out_fields": {
                            "type": "string",
                            "description": (
                                "Comma-separated field names to return."
                            ),
                            "default": "*",
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                "Maximum number of records (default 100)."
                            ),
                            "default": 100,
                            "minimum": 1,
                            "maximum": 1000,
                        },
                        "order_by": {
                            "type": "string",
                            "description": (
                                "Field name to sort by, optionally followed "
                                "by ASC or DESC (e.g. 'DateOfAdoption DESC')."
                            ),
                        },
                        "date_format": {
                            "type": "string",
                            "enum": ["date", "epoch"],
                            "description": (
                                "'date' (default) returns date fields as "
                                "YYYY-MM-DD; 'epoch' keeps raw millisecond "
                                "timestamps for data pipeline use. "
                                "Ignored when return_geometry=true "
                                "(GeoJSON responses already use ISO 8601)."
                            ),
                            "default": "date",
                        },
                        "return_geometry": {
                            "type": "boolean",
                            "description": (
                                "If true, each record includes a GeoJSON "
                                "geometry in WGS84 (EPSG:4326), "
                                "server-side simplified to ~5m. Default "
                                "false. When true, limit is capped at 50 "
                                "to guard against polygon payload bloat. "
                                "For point-in-polygon lookups, prefer "
                                "spatial_query_point."
                            ),
                            "default": False,
                        },
                    },
                    "required": ["item_id"],
                },
            ),
            ToolDefinition(
                name="spatial_query_point",
                description=(
                    f"Point-in-polygon lookup on a {city} polygon Feature "
                    f"Service. Given a lon/lat (WGS84), returns the "
                    f"attributes of every polygon feature that contains "
                    f"the point — e.g. 'which park is at this location?', "
                    f"'which zoning district?', 'which flood zone?'. Use "
                    f"get_layer_schema first to confirm the layer's "
                    f"geometryType is esriGeometryPolygon. Returns "
                    f"attributes only; polygon geometry is not included "
                    f"in the response."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": (
                                "ArcGIS Online item ID of a polygon "
                                "Feature Service."
                            ),
                        },
                        "lon": {
                            "type": "number",
                            "description": (
                                "Longitude in WGS84 decimal degrees "
                                "(-180 to 180). Note: lon comes first."
                            ),
                        },
                        "lat": {
                            "type": "number",
                            "description": (
                                "Latitude in WGS84 decimal degrees "
                                "(-90 to 90)."
                            ),
                        },
                        "where": {
                            "type": "string",
                            "description": (
                                "Optional SQL WHERE clause to further "
                                "filter candidate features."
                            ),
                            "default": "1=1",
                        },
                        "out_fields": {
                            "type": "string",
                            "description": (
                                "Comma-separated field names to return."
                            ),
                            "default": "*",
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                "Max features to return (default 10, "
                                "max 50). Point-in-polygon usually "
                                "returns 0–3 features."
                            ),
                            "default": 10,
                            "minimum": 1,
                            "maximum": 50,
                        },
                    },
                    "required": ["item_id", "lon", "lat"],
                },
            ),
            ToolDefinition(
                name="spatial_query_polygon",
                description=(
                    f"Server-side spatial intersection query on a {city} "
                    f"Feature Service. Given a polygon filter — either "
                    f"inline GeoJSON via filter_geometry, or a reference "
                    f"to polygon feature(s) in another layer via "
                    f"filter_item_id + filter_where — returns every "
                    f"target feature whose geometry intersects (or other "
                    f"spatial relation) the filter. Target layer may be "
                    f"polygon, polyline, or point. Use this instead of "
                    f"centroid-based assignments when features can "
                    f"straddle a boundary — e.g. 'which LRSA road "
                    f"segments fall within Assembly District 5?', "
                    f"'which trails cross this park?', 'which parcels "
                    f"touch this flood zone?'. Note: the ArcGIS REST "
                    f"'intersects' relation returns whole features — "
                    f"set return_geometry=true to retrieve GeoJSON "
                    f"geometries for precise client-side clipping at "
                    f"the filter boundary."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": (
                                "ArcGIS Online item ID of the target "
                                "layer (the layer to select features "
                                "from)."
                            ),
                        },
                        "filter_item_id": {
                            "type": "string",
                            "description": (
                                "Item ID of a polygon layer whose "
                                "feature(s) will be used as the filter "
                                "geometry. Combine with filter_where to "
                                "pick specific features. Use this when "
                                "the filter polygon already exists as a "
                                "published layer — avoids passing large "
                                "geometries through the model."
                            ),
                        },
                        "filter_where": {
                            "type": "string",
                            "description": (
                                "SQL WHERE clause applied to "
                                "filter_item_id to select which polygon "
                                "feature(s) to use as the filter. E.g. "
                                "\"DistrictName = 'District 5'\". All "
                                "matching features' rings are unioned."
                            ),
                            "default": "1=1",
                        },
                        "filter_geometry": {
                            "type": "object",
                            "description": (
                                "Inline GeoJSON Polygon, MultiPolygon, "
                                "or Feature wrapping one. Use this "
                                "instead of filter_item_id when you "
                                "already have the polygon in hand. "
                                "Coordinates must be WGS84 (EPSG:4326)."
                            ),
                        },
                        "spatial_rel": {
                            "type": "string",
                            "enum": [
                                "intersects",
                                "contains",
                                "within",
                                "crosses",
                                "touches",
                                "overlaps",
                                "envelope_intersects",
                            ],
                            "description": (
                                "ArcGIS spatial relation: 'intersects' "
                                "(any overlap, default), 'contains' "
                                "(filter contains target), 'within' "
                                "(target within filter), 'crosses', "
                                "'touches', 'overlaps', "
                                "'envelope_intersects'."
                            ),
                            "default": "intersects",
                        },
                        "where": {
                            "type": "string",
                            "description": (
                                "Optional SQL WHERE clause to further "
                                "filter target features by attribute."
                            ),
                            "default": "1=1",
                        },
                        "out_fields": {
                            "type": "string",
                            "description": (
                                "Comma-separated field names to return."
                            ),
                            "default": "*",
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                "Max features to return (default 50, "
                                "capped at 1000; capped at 50 when "
                                "return_geometry=true)."
                            ),
                            "default": 50,
                            "minimum": 1,
                            "maximum": 1000,
                        },
                        "return_geometry": {
                            "type": "boolean",
                            "description": (
                                "If true, each record includes a "
                                "GeoJSON geometry in WGS84, "
                                "server-side simplified to ~5m. "
                                "Useful for precise client-side "
                                "clipping. Limit is capped at 50."
                            ),
                            "default": False,
                        },
                    },
                    "required": ["item_id"],
                },
            ),
            ToolDefinition(
                name="aggregate_by_polygon",
                description=(
                    f"Bucket records from one {city} layer into polygons "
                    f"from another and return counts and sums per bucket. "
                    f"Use this instead of calling spatial_query_point in a "
                    f"loop. Answers questions like 'how many X per "
                    f"community council', 'total Y by assembly district', "
                    f"'cleanup tonnage by neighborhood'. If you find "
                    f"yourself calling spatial_query_point more than 5 "
                    f"times, switch to this. Returns a table of "
                    f"count + summed numeric fields per bucket, plus an "
                    f"unmatched count for data-quality signal."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "source_item_id": {
                            "type": "string",
                            "description": (
                                "ArcGIS item ID of the layer whose "
                                "records you want to bucket (points or "
                                "polygons)."
                            ),
                        },
                        "aggregation_item_id": {
                            "type": "string",
                            "description": (
                                "ArcGIS item ID of the polygon layer "
                                "to bucket into (e.g. community "
                                "councils, assembly districts)."
                            ),
                        },
                        "group_by_field": {
                            "type": "string",
                            "description": (
                                "Field on the aggregation layer whose "
                                "value labels each bucket (e.g. "
                                "'COUNCIL', 'DISTRICT_NAME')."
                            ),
                        },
                        "sum_fields": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Numeric field names from the source "
                                "layer to sum per bucket. Omit for "
                                "count-only."
                            ),
                        },
                        "count": {
                            "type": "boolean",
                            "description": (
                                "Include a feature count per bucket "
                                "(default true)."
                            ),
                            "default": True,
                        },
                        "source_where": {
                            "type": "string",
                            "description": (
                                "SQL WHERE clause applied to the source "
                                "layer before aggregating."
                            ),
                            "default": "1=1",
                        },
                        "agg_where": {
                            "type": "string",
                            "description": (
                                "SQL WHERE clause to narrow the "
                                "aggregation polygons (e.g. exclude "
                                "retired districts)."
                            ),
                            "default": "1=1",
                        },
                        "centroid_mode": {
                            "type": "string",
                            "enum": [
                                "auto",
                                "centroid",
                                "representative_point",
                            ],
                            "description": (
                                "How to reduce source polygons to a "
                                "point for the join. 'centroid' is "
                                "cheap; 'representative_point' stays "
                                "inside L-shaped/donut polygons; "
                                "'auto' (default) uses centroid unless "
                                "it falls outside, then "
                                "representative_point."
                            ),
                            "default": "auto",
                        },
                        "overlap_policy": {
                            "type": "string",
                            "enum": [
                                "first_match",
                                "all_matches",
                                "largest",
                            ],
                            "description": (
                                "How to handle source points that fall "
                                "inside overlapping aggregation "
                                "polygons. 'first_match' (default) is "
                                "fastest; 'all_matches' double-counts "
                                "but is honest; 'largest' picks the "
                                "biggest polygon deterministically."
                            ),
                            "default": "first_match",
                        },
                        "max_source_features": {
                            "type": "integer",
                            "description": (
                                "Safety cap on source features pulled "
                                "(default 5000, max 5000). Narrow "
                                "source_where if you hit the cap."
                            ),
                            "default": 5000,
                        },
                    },
                    "required": [
                        "source_item_id",
                        "aggregation_item_id",
                        "group_by_field",
                    ],
                },
            ),
            ToolDefinition(
                name="filter_by_polygon",
                description=(
                    f"Return the subset of records from one {city} layer "
                    f"that fall inside a named polygon (or polygons) "
                    f"from another layer. Use this for questions like "
                    f"'what are the reports in Fairview', 'show me the "
                    f"cleanups in Midtown'. The polygon is identified by "
                    f"SQL, not coordinates — never ask the user for "
                    f"lat/lon when a polygon name will do. If "
                    f"container_where matches 0 polygons, returns a "
                    f"clear error (not a silently empty result). "
                    f"Multi-polygon containers (e.g. "
                    f"COUNCIL IN ('A','B','C')) are unioned."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "source_item_id": {
                            "type": "string",
                            "description": (
                                "ArcGIS item ID of the layer to filter."
                            ),
                        },
                        "container_item_id": {
                            "type": "string",
                            "description": (
                                "ArcGIS item ID of the polygon layer "
                                "that provides the named container "
                                "(e.g. a community-councils layer)."
                            ),
                        },
                        "container_where": {
                            "type": "string",
                            "description": (
                                "SQL to pick the container polygon(s), "
                                "e.g. \"COUNCIL='Fairview'\" or "
                                "\"COUNCIL IN ('Midtown','Fairview')\". "
                                "Must match ≥1 polygon; 0 matches is "
                                "reported as an error."
                            ),
                        },
                        "source_where": {
                            "type": "string",
                            "description": (
                                "Optional SQL WHERE clause applied to "
                                "source records as well."
                            ),
                            "default": "1=1",
                        },
                        "out_fields": {
                            "type": "string",
                            "description": (
                                "Comma-separated field names to return."
                            ),
                            "default": "*",
                        },
                        "return_geometry": {
                            "type": "boolean",
                            "description": (
                                "If true, each record includes GeoJSON "
                                "geometry (WGS84, simplified). Limit "
                                "caps at 50 when true."
                            ),
                            "default": False,
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                "Max features to return (default 100, "
                                "max 1000; 50 when return_geometry=true)."
                            ),
                            "default": 100,
                        },
                    },
                    "required": [
                        "source_item_id",
                        "container_item_id",
                        "container_where",
                    ],
                },
            ),
        ]

    # ── Tool dispatch ─────────────────────────────────────────────────────

    async def execute_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> ToolResult:
        try:
            if tool_name == "find_gis_content":
                text = await self._find_gis_content(arguments)

            elif tool_name == "browse_gallery":
                text = await self._browse_gallery(
                    arguments.get("keyword", "").strip(),
                    min(int(arguments.get("limit", 20)), 100),
                )

            elif tool_name == "search_spatial_layers":
                query = arguments.get("query", "").strip()
                if not query:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="query is required",
                    )
                text = await self._search_spatial_layers(
                    query,
                    arguments.get("layer_type", "all"),
                    min(int(arguments.get("limit", 10)), 50),
                )

            elif tool_name == "get_item_details":
                item_id = arguments.get("item_id", "").strip()
                if not item_id:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="item_id is required",
                    )
                item = await self.get_dataset(item_id)
                text = self._format_details(item)

            elif tool_name == "get_layer_schema":
                text = await self._get_layer_schema(arguments)

            elif tool_name == "search_layers_by_field":
                text = await self._search_layers_by_field(arguments)

            elif tool_name == "query_data":
                item_id = arguments.get("item_id", "").strip()
                if not item_id:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="item_id is required",
                    )
                where = arguments.get("where", "1=1")
                out_fields = arguments.get("out_fields", "*")
                order_by = arguments.get("order_by", "")
                date_format = arguments.get("date_format", "date")
                return_geometry = bool(
                    arguments.get("return_geometry", False)
                )
                requested_limit = int(arguments.get("limit", 100))
                effective_limit = (
                    min(requested_limit, self.GEOMETRY_LIMIT_CAP)
                    if return_geometry
                    else requested_limit
                )
                filters = {
                    "where": where,
                    "out_fields": out_fields,
                    "order_by": order_by,
                }

                # Resolve service URL once for parallel queries
                item = await self.get_dataset(item_id)
                service_url = self._ensure_layer_url(
                    item.get("url", "")
                )
                validated_where = WhereValidator.validate(where)

                parallel_tasks = [
                    self.query_data(
                        item_id,
                        filters,
                        effective_limit,
                        return_geometry=return_geometry,
                    ),
                    self._get_record_count(service_url, validated_where),
                ]
                # When return_geometry=true the backend is f=geojson,
                # which renders dates as ISO strings already — skip the
                # epoch-to-date conversion path.
                want_dates = (
                    date_format != "epoch" and not return_geometry
                )
                if want_dates:
                    parallel_tasks.append(
                        self._get_date_fields(service_url)
                    )

                results = await asyncio.gather(*parallel_tasks)
                records = results[0]
                total_count = results[1]
                date_fields = results[2] if want_dates else None

                text = self._format_query_results(
                    records, effective_limit, total_count, date_fields
                )

            elif tool_name == "spatial_query_point":
                item_id = arguments.get("item_id", "").strip()
                if not item_id:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="item_id is required",
                    )
                if "lon" not in arguments or "lat" not in arguments:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="lon and lat are required",
                    )
                limit = min(int(arguments.get("limit", 10)), 50)
                records = await self.spatial_query_point(
                    item_id,
                    lon=arguments["lon"],
                    lat=arguments["lat"],
                    filters={
                        "where": arguments.get("where", "1=1"),
                        "out_fields": arguments.get("out_fields", "*"),
                    },
                    limit=limit,
                )
                if not records:
                    text = (
                        f"No features in item `{item_id}` contain point "
                        f"(lon={arguments['lon']}, lat={arguments['lat']})."
                    )
                else:
                    # total_count=None avoids a misleading "of N total"
                    # line: every match is already in `records`, there
                    # is no paging going on.
                    text = self._format_query_results(
                        records, limit, total_count=None, date_fields=None
                    )

            elif tool_name == "spatial_query_polygon":
                item_id = arguments.get("item_id", "").strip()
                if not item_id:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="item_id is required",
                    )
                filter_geometry = arguments.get("filter_geometry")
                filter_item_id = (
                    arguments.get("filter_item_id") or ""
                ).strip() or None
                if not filter_geometry and not filter_item_id:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message=(
                            "spatial_query_polygon requires either "
                            "filter_geometry (inline GeoJSON) or "
                            "filter_item_id"
                        ),
                    )
                return_geometry = bool(
                    arguments.get("return_geometry", False)
                )
                requested_limit = int(arguments.get("limit", 50))
                effective_limit = (
                    min(requested_limit, self.GEOMETRY_LIMIT_CAP)
                    if return_geometry
                    else min(requested_limit, 1000)
                )
                records = await self.spatial_query_polygon(
                    item_id,
                    filter_geometry=filter_geometry,
                    filter_item_id=filter_item_id,
                    filter_where=arguments.get("filter_where", "1=1"),
                    spatial_rel=arguments.get("spatial_rel", "intersects"),
                    filters={
                        "where": arguments.get("where", "1=1"),
                        "out_fields": arguments.get("out_fields", "*"),
                    },
                    limit=effective_limit,
                    return_geometry=return_geometry,
                )
                if not records:
                    text = (
                        f"No features in item `{item_id}` match the "
                        f"filter polygon."
                    )
                else:
                    text = self._format_query_results(
                        records,
                        effective_limit,
                        total_count=None,
                        date_fields=None,
                    )

            elif tool_name == "aggregate_by_polygon":
                text = await self._aggregate_by_polygon(arguments)

            elif tool_name == "filter_by_polygon":
                text = await self._filter_by_polygon(arguments)

            else:
                return ToolResult(
                    content=[],
                    success=False,
                    error_message=f"Unknown tool: {tool_name}",
                )

            return ToolResult(
                content=[{"type": "text", "text": text}],
                success=True,
            )

        except Exception as e:
            logger.error(
                f"Error executing tool {tool_name}: {e}", exc_info=True
            )
            return ToolResult(
                content=[],
                success=False,
                error_message=str(e) if str(e) else "Tool execution failed",
            )

    # ── Health check ──────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        try:
            resp = await self.client.get(
                f"{self.plugin_config.portal_base_url}/search",
                params={
                    "q": f"orgid:{self.plugin_config.org_id}",
                    "f": "json",
                    "num": "1",
                },
            )
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False

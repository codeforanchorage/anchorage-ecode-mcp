"""Smoke test the new aggregate_by_polygon / filter_by_polygon tools
against the real MOA portal."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugins.anchorage_gis.plugin import AnchorageGISPlugin  # noqa: E402


CONFIG = {
    "portal_base_url": "https://muniorg.maps.arcgis.com/sharing/rest",
    "gallery_group_id": "c34ed10758ec4f4eb8aa6826ee5be3ff",
    "org_id": "Ce3DhLRthdwbHlfF",
    "city_name": "Municipality of Anchorage",
    "gallery_url": (
        "https://muniorg.maps.arcgis.com/apps/instant/filtergallery/"
        "index.html?appid=4dac7569f1cc4beb9f22ce168c899a30"
    ),
    "timeout": 30,
}

SOURCE_CLEANING = "0c329ff76ae24faeb76a9ad14eca39af"
COUNCILS = "934783d347ee4df5a0c12bd2d0339045"


async def main() -> None:
    plugin = AnchorageGISPlugin(CONFIG)
    ok = await plugin.initialize()
    assert ok, "plugin failed to initialize"
    try:
        print("\n### aggregate_by_polygon (cleanings by COUNCIL) ###\n")
        r = await plugin.execute_tool(
            "aggregate_by_polygon",
            {
                "source_item_id": SOURCE_CLEANING,
                "aggregation_item_id": COUNCILS,
                "group_by_field": "COUNCIL",
                "sum_fields": ["Total_Pounds", "Total_LaborHours"],
            },
        )
        print("success=", r.success, "err=", r.error_message)
        print(r.content[0]["text"] if r.content else "(no content)")

        print("\n### filter_by_polygon (cleanings in Fairview) ###\n")
        r2 = await plugin.execute_tool(
            "filter_by_polygon",
            {
                "source_item_id": SOURCE_CLEANING,
                "container_item_id": COUNCILS,
                "container_where": "COUNCIL='Fairview'",
                "limit": 5,
            },
        )
        print("success=", r2.success, "err=", r2.error_message)
        print(r2.content[0]["text"] if r2.content else "(no content)")

        print("\n### filter_by_polygon (typo → 0-polygon error) ###\n")
        r3 = await plugin.execute_tool(
            "filter_by_polygon",
            {
                "source_item_id": SOURCE_CLEANING,
                "container_item_id": COUNCILS,
                "container_where": "COUNCIL='Fairveiw'",
                "limit": 5,
            },
        )
        print("success=", r3.success, "err=", r3.error_message)
        print(r3.content[0]["text"] if r3.content else "(no content)")
    finally:
        await plugin.shutdown()


if __name__ == "__main__":
    asyncio.run(main())

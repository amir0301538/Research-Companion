"""
ERD schema definitions for the entities populated from a Google Scholar profile.

Column names are copied EXACTLY (spelling, case, underscores, order) from
"Research_Companion_ERD_Listing_Exact_Names_V2.docx". At runtime the scraper
re-reads the DOCX (see docx_schema.py) and aborts if these lists ever drift
from the document, so CSV headers can never silently diverge from the ERD.
"""

from collections import OrderedDict

# Entity name (exactly as written in the DOCX) -> ordered list of columns.
ERD_ENTITIES = OrderedDict(
    [
        (
            "HEIs",
            [
                "hei_id",
                "hei_name",
                "short_name",
                "city",
                "country",
                "website_url",
                "target_university",
                "created_at",
            ],
        ),
        (
            "SCHOLARS",
            [
                "scholar_id",
                "hei_id",
                "google_scholar_id",
                "scholar_name",
                "email",
                "google_scholar_profile_url",
                "h_index",
                "total_publications",
                "total_citations",
                "last_synced_at",
            ],
        ),
        (
            "RESEARCH_AREAS",
            [
                "research_area_id",
                "area_name",
                "description",
                "is_computing_area",
                "keywords",
                "created_at",
            ],
        ),
        (
            "PUBLICATIONS",
            [
                "publication_id",
                "title",
                "publication_year",
                "citation_count",
                "venue",
                "authors_text",
                "digital_object_identifier",
                "source_url",
                "last_synced_at",
            ],
        ),
        (
            "Scholar Research Areas",
            [
                "scholar_id",
                "research_area_id",
                "is_primary_area",
                "area_source",
                "updated_at",
            ],
        ),
        (
            "Authors Publications",
            [
                "scholar_id",
                "publication_id",
                "author_order",
                "source_method",
                "recorded_at",
            ],
        ),
    ]
)

# Entity -> CSV file written to the output folder.
# HEIs is intentionally NOT exported: the profile front page shows only a
# free-text affiliation line, which cannot reliably populate any HEIs column.
# Its attributes are still assessed in extraction_report.txt.
CSV_FILES = OrderedDict(
    [
        ("SCHOLARS", "scholars.csv"),
        ("RESEARCH_AREAS", "research_areas.csv"),
        ("PUBLICATIONS", "publications.csv"),
        ("Scholar Research Areas", "scholar_research_areas.csv"),
        ("Authors Publications", "authors_publications.csv"),
    ]
)

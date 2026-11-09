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
# All six database entities in scope for the Google Scholar profile scraper
# are exported with exact DOCX column names and order.
CSV_FILES = OrderedDict(
    [
        ("HEIs", "heis.csv"),
        ("SCHOLARS", "scholars.csv"),
        ("PUBLICATIONS", "publications.csv"),
        ("RESEARCH_AREAS", "research_areas.csv"),
        ("Scholar Research Areas", "scholar_research_areas.csv"),
        ("Authors Publications", "authors_publications.csv"),
    ]
)

# Extended / proposed Stage-2 publication detail entities written to
# output/supplementary/ while keeping the 6 core DOCX-verified CSVs intact.
DETAIL_ENTITIES = OrderedDict(
    [
        (
            "PUBLICATION_DETAILS",
            [
                "publication_id",
                "scholar_id",
                "title",
                "authors_text",
                "publication_date",
                "publication_year",
                "journal",
                "volume",
                "issue",
                "pages",
                "publisher",
                "description",
                "citation_count",
                "publication_url",
                "publisher_url",
                "doi",
                "last_synced_at",
            ],
        ),
        (
            "PUBLICATION_YEARLY_CITATIONS",
            [
                "publication_id",
                "citation_year",
                "citation_count",
                "last_synced_at",
            ],
        ),
    ]
)

DETAIL_CSV_FILES = OrderedDict(
    [
        ("PUBLICATION_DETAILS", "publication_details.csv"),
        ("PUBLICATION_YEARLY_CITATIONS", "publication_yearly_citations.csv"),
    ]
)

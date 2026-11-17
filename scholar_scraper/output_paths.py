"""Single location map for persistent scraper storage and generated artifacts."""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OutputPaths:
    root: Path

    def __post_init__(self):
        object.__setattr__(self, "root", Path(self.root))

    @property
    def csv_dir(self): return self.root / "csv"

    @property
    def json_dir(self): return self.root / "json"

    @property
    def images_dir(self): return self.root / "profile_images"

    @property
    def reports_dir(self): return self.root / "reports"

    @property
    def logs_dir(self): return self.root / "logs"

    @property
    def system_dir(self): return self.root / "system"

    @property
    def registry(self): return self.system_dir / "id_registry.json"

    @property
    def lock(self): return self.system_dir / ".dataset.lock"

    @property
    def json_export(self): return self.json_dir / "dataset.json"

    @property
    def image_metadata(self): return self.csv_dir / "scholar_images.csv"

    @property
    def linkage(self): return self.system_dir / "record_linkage.csv"

    @property
    def historical(self): return self.system_dir / "historical"

    @property
    def checkpoints(self): return self.system_dir / "checkpoints"

    @property
    def snapshots(self): return self.system_dir / "snapshots"

    def csv(self, filename): return self.csv_dir / filename

    def report(self, filename): return self.reports_dir / filename

    def log(self, filename="scraper.log"): return self.logs_dir / filename

    def profile_snapshot(self, google_id): return self.snapshots / "profiles" / f"{google_id}.json"

    def relative(self, path):
        return Path(path).relative_to(self.root).as_posix()

    def ensure_dirs(self):
        for path in (self.csv_dir, self.json_dir, self.images_dir, self.reports_dir,
                     self.logs_dir, self.system_dir, self.checkpoints, self.snapshots,
                     self.historical):
            path.mkdir(parents=True, exist_ok=True)

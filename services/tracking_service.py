import json
import os
from services.logger import get_logger

logger = get_logger(__name__)

class TrackingService:
    def __init__(self, stats_file='logs/execution_stats.json'):
        self.stats_file = stats_file
        self.stats = self._load()

    def _load(self):
        if os.path.exists(self.stats_file):
            with open(self.stats_file, 'r') as f:
                return json.load(f)
        return {'total_processed': 0, 'sent': 0, 'skipped': 0, 'errors': []}

    def _save(self):
        os.makedirs(os.path.dirname(self.stats_file), exist_ok=True)
        with open(self.stats_file, 'w') as f:
            json.dump(self.stats, f, indent=2)

    def increment(self, key, amount=1):
        self.stats[key] = self.stats.get(key, 0) + amount
        self._save()

    def add_error(self, error):
        self.stats['errors'].append(error)
        self._save()

    def get_stats(self):
        return self.stats

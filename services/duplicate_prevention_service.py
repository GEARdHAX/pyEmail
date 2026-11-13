import os
import json
from services.logger import get_logger

logger = get_logger(__name__)

class DuplicatePreventionService:
    def __init__(self, tracking_file='logs/tracking.json'):
        self.tracking_file = tracking_file
        self.sent = self._load()

    def _load(self):
        if os.path.exists(self.tracking_file):
            with open(self.tracking_file, 'r') as f:
                return json.load(f)
        return {}

    def _save(self):
        os.makedirs(os.path.dirname(self.tracking_file), exist_ok=True)
        with open(self.tracking_file, 'w') as f:
            json.dump(self.sent, f, indent=2)

    def is_sent(self, recipient):
        return recipient in self.sent

    def mark_sent(self, recipient, message_id):
        self.sent[recipient] = message_id
        self._save()
        logger.info(f"Marked {recipient} as sent with message ID {message_id}")
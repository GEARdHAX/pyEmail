import re
import os
from services.logger import get_logger

logger = get_logger(__name__)

class EmailTemplateService:
    def __init__(self, templates_dir='templates'):
        self.templates_dir = templates_dir
        self.cache = {}

    def load_template(self, name, kind='html'):
        path = os.path.join(self.templates_dir, f"{name}.{kind}")
        if not os.path.exists(path):
            logger.error(f"Template not found: {path}")
            raise FileNotFoundError(f"Template not found: {path}")
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()

    def render(self, template, context):
        missing = []
        for key, value in context.items():
            if value is None or value == '':
                missing.append(key)
        if missing:
            logger.warning(f"Missing values for placeholders: {missing}")
            return None, missing

        rendered = template
        for key, value in context.items():
            rendered = rendered.replace(f"{{{{{key}}}}}", str(value))
        return rendered, None

    def validate_recipient(self, email):
        pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        return bool(re.match(pattern, email))

    PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")

    @classmethod
    def placeholders(cls, text):
        """Placeholder names in order of first appearance."""
        seen = []
        for name in cls.PLACEHOLDER_RE.findall(text):
            if name not in seen:
                seen.append(name)
        return seen

    @classmethod
    def render_strict(cls, text, values):
        """Substitute every {{placeholder}} verbatim (no other text is touched).

        Returns (rendered, missing). `missing` lists placeholders with no value
        or a blank value; in that case rendered is None.
        """
        names = cls.placeholders(text)
        missing = [n for n in names if not str(values.get(n, "")).strip()]
        if missing:
            return None, missing
        rendered = cls.PLACEHOLDER_RE.sub(lambda m: str(values[m.group(1)]).strip(), text)
        leftover = cls.placeholders(rendered)
        if leftover:  # a cell value itself contained {{...}}
            return None, leftover
        return rendered, None

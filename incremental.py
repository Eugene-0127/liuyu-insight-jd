"""Conservative early stop; this is an overlap check, not a delta API."""
import re
from datetime import datetime


class Frontier:
    def __init__(self, known=(), enabled=False):
        self.known=set(known)
        self.enabled=enabled and bool(self.known)
        self.previous=None
        self.old_pages=0
        self.seen=set()
        self.reached=False

    def feed(self, rows):
        if not self.enabled or not rows:return
        signature=tuple(str(r.get('platform_id') or '') for r in rows)
        if signature in self.seen:return
        self.seen.add(signature)
        all_old=True
        for r in rows:
            raw=str(r.get('review_date') or '')
            if not re.match(r'^\d{4}-\d{2}-\d{2}',raw):
                self.enabled=False;return
            try:stamp=datetime.fromisoformat(raw.replace('Z','+00:00')).replace(tzinfo=None)
            except ValueError:self.enabled=False;return
            if self.previous is not None and stamp>self.previous:
                self.enabled=False;return
            self.previous=stamp
            if not r.get('platform_id') or str(r['platform_id']) not in self.known:all_old=False
        self.old_pages=self.old_pages+1 if all_old else 0
        self.reached=self.old_pages>=2

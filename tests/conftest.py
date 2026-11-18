import os
import sys
from pathlib import Path

# Make `import scandesk` work when running pytest from a source checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

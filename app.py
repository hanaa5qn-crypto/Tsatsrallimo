import importlib.util
from pathlib import Path


BACKEND_APP = Path(__file__).resolve().parent / "middle man project" / "backend" / "app.py"
spec = importlib.util.spec_from_file_location("tsatsral_limo_backend_app", BACKEND_APP)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)

app = module.app

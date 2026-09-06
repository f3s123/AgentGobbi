import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'api'))
os.environ['APP_ENV'] = 'test'
os.environ['APP_ORIGIN'] = 'http://testserver'
os.environ['GOOGLE_API_KEY'] = ''

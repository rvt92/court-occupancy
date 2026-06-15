import yaml
import os

with open("env.yaml") as f:
    env = yaml.safe_load(f)

for key, value in env.items():
    os.environ[key] = str(value)

from main_v3_github_actions import run
run()

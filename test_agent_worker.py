#!/usr/bin/env python3
"""Simple test to verify agent worker can be imported and instantiated."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

try:
    from agent_worker import AgentWorker
    print("✓ AgentWorker imported successfully")

    # Test instantiation with a manifest path
    manifest_path = "manifests/coder-python-fast.json"
    if os.path.exists(manifest_path):
        worker = AgentWorker(manifest_path)
        print("✓ AgentWorker instantiated successfully")
        print(f"  Agent ID: {worker.agent_id}")
    else:
        print(f"✗ Manifest not found: {manifest_path}")

except Exception as e:
    print(f"✗ Error: {e}")
    import traceback
    traceback.print_exc()
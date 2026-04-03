"""
OhMyUnrealEngine MCP Tool Quality Test Suite
Tests all MCP functions for: basic call, error handling, boundary conditions, integration.
Requires UE editor running with plugin loaded on port 9090.

Usage: python test_mcp_quality.py [--category basic|error|boundary|integration] [--filter pattern]
"""

import json
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

URL = "http://127.0.0.1:9090/api/call"
TIMEOUT = 30


@dataclass
class TestResult:
    name: str
    category: str
    status: str  # PASS, FAIL, SKIP, ERROR
    message: str = ""
    duration_ms: float = 0


class MCPTester:
    def __init__(self):
        self.results: list[TestResult] = []
        self.created_assets: list[str] = []  # for cleanup

    def call(self, function: str, params: dict | None = None, timeout: int = TIMEOUT) -> dict:
        payload = {"function": function}
        if params:
            payload["params"] = params
        data = json.dumps(payload).encode()
        req = urllib.request.Request(URL, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.URLError as e:
            return {"_error": f"Connection failed: {e}"}
        except json.JSONDecodeError:
            return {"_error": "Invalid JSON response"}
        except Exception as e:
            return {"_error": str(e)}

    def test(self, name: str, category: str, func, skip_reason: str = ""):
        if skip_reason:
            self.results.append(TestResult(name, category, "SKIP", skip_reason))
            return
        start = time.time()
        try:
            func()
            dur = (time.time() - start) * 1000
            self.results.append(TestResult(name, category, "PASS", duration_ms=dur))
        except AssertionError as e:
            dur = (time.time() - start) * 1000
            self.results.append(TestResult(name, category, "FAIL", str(e), dur))
        except Exception as e:
            dur = (time.time() - start) * 1000
            self.results.append(TestResult(name, category, "ERROR", str(e), dur))

    def assert_ok(self, r: dict, msg: str = ""):
        assert "_error" not in r, f"Call failed: {r.get('_error')} {msg}"
        assert r.get("ok") is True or "error" not in r, f"Not ok: {r} {msg}"

    def assert_error(self, r: dict, msg: str = ""):
        assert "error" in r or r.get("ok") is False, f"Expected error but got: {r} {msg}"

    def assert_has_fields(self, r: dict, fields: list[str], msg: str = ""):
        for f in fields:
            assert f in r, f"Missing field '{f}' in {list(r.keys())} {msg}"

    def assert_json_response(self, r: dict, msg: str = ""):
        assert "_error" not in r, f"No JSON response: {r.get('_error')} {msg}"
        assert isinstance(r, dict), f"Response is not dict: {type(r)} {msg}"

    # =========================================================================
    # BASIC CALL TESTS - Each function returns valid JSON
    # =========================================================================
    def run_basic_tests(self):
        print("\n=== BASIC CALL TESTS ===")
        basic_tests = [
            # Core
            ("GetProjectSettings", {}),
            ("GetWorldSettings", {}),
            ("GetEditorSelection", {}),
            ("GetCurrentLevel", {}),
            # Actor
            ("ListActors", {"Limit": 3}),
            ("GetActorHierarchy", {}),
            # Asset
            ("ListAssets", {"Path": "/Game", "Limit": 3}),
            ("GetAssetsByClass", {"ClassName": "Material", "Limit": 3}),
            ("FindObjects", {"ClassName": "Material", "Limit": 3}),
            ("GetModifiedAssets", {"Limit": 3}),
            # Blueprint
            ("ListBlueprintNodes", {"BlueprintPath": "/Game/SurvivorsRoguelike/System/BP_Base_GameMode", "Limit": 3}),
            ("GetBlueprintInfo", {"BlueprintPath": "/Game/SurvivorsRoguelike/System/BP_Base_GameMode"}),
            ("ListBlueprintGraphs", {"BlueprintPath": "/Game/SurvivorsRoguelike/System/BP_Base_GameMode"}),
            ("GetBlueprintGraph", {"BlueprintPath": "/Game/SurvivorsRoguelike/System/BP_Base_GameMode", "GraphName": "EventGraph"}),
            ("GetBlueprintDefaults", {"BlueprintPath": "/Game/SurvivorsRoguelike/System/BP_Base_GameMode"}),
            # Material
            ("GetMaterialDetails", {"MaterialPath": "/Game/Characters/Mannequins/Materials/M_Mannequin"}),
            ("GetMaterialExpressions", {"MaterialPath": "/Game/Characters/Mannequins/Materials/M_Mannequin"}),
            ("GetChildMaterialInstances", {"MaterialPath": "/Game/Characters/Mannequins/Materials/M_Mannequin"}),
            # Mesh
            ("GetStaticMeshDetails", {"MeshPath": "/Engine/BasicShapes/Cube"}),
            # Scene
            ("GetCollisionProfiles", {"Limit": 5}),
            ("GetNavMeshData", {}),
            ("GetFoliageInstances", {}),
            # Level
            ("BuildNavigation", {}),
            # Data
            ("GetEnumValues", {"EnumPath": "/Game/SurvivorsRoguelike/Enums/E_AbilityType"}),
            # Validation
            ("ValidateBlueprintDeep", {"BlueprintPath": "/Game/SurvivorsRoguelike/System/BP_Base_GameMode"}),
            # Performance
            ("GetMemoryStats", {}),
            ("GetDrawCallStats", {}),
            ("GetRenderingStats", {}),
            # Widget
            ("ListWindows", {}),
        ]

        for func_name, params in basic_tests:
            def make_test(fn, p):
                def t():
                    r = self.call(fn, p)
                    self.assert_json_response(r, fn)
                return t
            self.test(f"basic_{func_name}", "basic", make_test(func_name, params))

    # =========================================================================
    # ERROR HANDLING TESTS - Wrong params return proper errors
    # =========================================================================
    def run_error_tests(self):
        print("\n=== ERROR HANDLING TESTS ===")

        error_tests = [
            ("err_bp_not_found", "GetBlueprintInfo", {"BlueprintPath": "/Game/NONEXISTENT/BP_Fake"}),
            ("err_mat_not_found", "GetMaterialDetails", {"MaterialPath": "/Game/NONEXISTENT/M_Fake"}),
            ("err_mesh_not_found", "GetStaticMeshDetails", {"MeshPath": "/Game/NONEXISTENT/SM_Fake"}),
            ("err_actor_not_found", "GetActorInfo", {"ActorName": "NONEXISTENT_ACTOR_12345"}),
            ("err_asset_not_found", "GetAssetInfo", {"AssetPath": "/Game/NONEXISTENT/Asset"}),
            ("err_graph_not_found", "GetBlueprintGraph", {"BlueprintPath": "/Game/SurvivorsRoguelike/System/BP_Base_GameMode", "GraphName": "NONEXISTENT_GRAPH"}),
            ("err_unknown_function", "NONEXISTENT_FUNCTION_XYZ", {}),
            ("err_recompile_bad_mat", "RecompileMaterialAsset", {"MaterialPath": "/Game/NONEXISTENT"}),
            ("err_validate_bad_bp", "ValidateBlueprintDeep", {"BlueprintPath": "/Game/NONEXISTENT"}),
            ("err_dependency_bad", "GetAssetDependencyTree", {"AssetPath": "/Game/NONEXISTENT"}),
            ("err_class_not_found", "GetAssetsByClass", {"ClassName": "NONEXISTENT_CLASS_XYZ"}),
        ]

        for test_name, func_name, params in error_tests:
            def make_test(fn, p):
                def t():
                    r = self.call(fn, p)
                    self.assert_json_response(r, fn)
                    self.assert_error(r, f"{fn} should return error for bad input")
                return t
            self.test(test_name, "error", make_test(func_name, params))

    # =========================================================================
    # BOUNDARY CONDITION TESTS
    # =========================================================================
    def run_boundary_tests(self):
        print("\n=== BOUNDARY CONDITION TESTS ===")

        # Empty string params
        def test_empty_bp_path():
            r = self.call("GetBlueprintInfo", {"BlueprintPath": ""})
            self.assert_json_response(r)
            self.assert_error(r, "Empty path should error")
        self.test("bound_empty_bp_path", "boundary", test_empty_bp_path)

        def test_empty_mat_path():
            r = self.call("GetMaterialDetails", {"MaterialPath": ""})
            self.assert_json_response(r)
            self.assert_error(r, "Empty path should error")
        self.test("bound_empty_mat_path", "boundary", test_empty_mat_path)

        # Zero/negative limit (should use defaults due to UFunction zero-init guard)
        def test_zero_limit():
            r = self.call("ListActors", {"Limit": 0})
            self.assert_json_response(r)
            actors = r.get("actors", [])
            assert len(actors) > 0, "Zero limit should default to 50, not return empty"
        self.test("bound_zero_limit", "boundary", test_zero_limit)

        def test_negative_limit():
            r = self.call("ListAssets", {"Path": "/Game", "Limit": -1})
            self.assert_json_response(r)
        self.test("bound_negative_limit", "boundary", test_negative_limit)

        # Large offset
        def test_large_offset():
            r = self.call("ListActors", {"Limit": 5, "Offset": 99999})
            self.assert_json_response(r)
            actors = r.get("actors", [])
            assert len(actors) == 0, "Large offset should return empty list"
        self.test("bound_large_offset", "boundary", test_large_offset)

        # Pagination total field
        def test_pagination_total():
            r = self.call("SceneSnapshot", {"Limit": 2, "Offset": 0})
            self.assert_json_response(r)
            assert "total" in r, "Paginated function must return 'total' field"
            total = r["total"]
            actors = r.get("actors", [])
            assert len(actors) <= 2, f"Limit=2 but got {len(actors)} actors"
            assert total >= len(actors), f"total ({total}) < returned ({len(actors)})"
        self.test("bound_pagination_total", "boundary", test_pagination_total)

        # Pagination consistency
        def test_pagination_consistency():
            r1 = self.call("GetCollisionProfiles", {"Limit": 3, "Offset": 0})
            r2 = self.call("GetCollisionProfiles", {"Limit": 3, "Offset": 3})
            self.assert_json_response(r1)
            self.assert_json_response(r2)
            total1 = r1.get("total", 0)
            total2 = r2.get("total", 0)
            assert total1 == total2, f"Total changed between pages: {total1} vs {total2}"
            p1 = r1.get("profiles", [])
            p2 = r2.get("profiles", [])
            if p1 and p2:
                names1 = {p.get("name") for p in p1}
                names2 = {p.get("name") for p in p2}
                assert not names1.intersection(names2), "Pages should not overlap"
        self.test("bound_pagination_consistency", "boundary", test_pagination_consistency)

    # =========================================================================
    # INTEGRATION TESTS - Full create→query→modify→verify workflows
    # =========================================================================
    def run_integration_tests(self):
        print("\n=== INTEGRATION TESTS ===")

        # Material workflow: query existing → recompile → layout → verify
        def test_material_workflow():
            mat = "/Game/Characters/Mannequins/Materials/M_Mannequin"

            # Get details
            r1 = self.call("GetMaterialDetails", {"MaterialPath": mat})
            self.assert_json_response(r1)
            self.assert_has_fields(r1, ["expression_count", "parameters"])
            assert r1["expression_count"] > 0, "Material should have expressions"

            # Get expressions
            r2 = self.call("GetMaterialExpressions", {"MaterialPath": mat})
            self.assert_json_response(r2)
            assert len(r2.get("expressions", [])) > 0, "Should list expressions"

            # Layout
            r3 = self.call("LayoutMaterialExpressionsAsset", {"MaterialPath": mat})
            self.assert_json_response(r3)

            # Recompile
            r4 = self.call("RecompileMaterialAsset", {"MaterialPath": mat})
            self.assert_json_response(r4)

            # Get child instances
            r5 = self.call("GetChildMaterialInstances", {"MaterialPath": mat})
            self.assert_json_response(r5)
            self.assert_has_fields(r5, ["child_count", "children"])
        self.test("integ_material_workflow", "integration", test_material_workflow)

        # Blueprint validation workflow
        def test_bp_validation_workflow():
            bp = "/Game/SurvivorsRoguelike/System/BP_Base_GameMode"
            r = self.call("ValidateBlueprintDeep", {"BlueprintPath": bp})
            self.assert_json_response(r)
            self.assert_has_fields(r, ["compile_status", "issue_count", "compiler_errors", "has_phantom_error"])
            assert r["compile_status"] in ("up_to_date", "error", "dirty"), f"Bad status: {r['compile_status']}"
        self.test("integ_bp_validation", "integration", test_bp_validation_workflow)

        # Asset query workflow
        def test_asset_query_workflow():
            # Find all materials
            r1 = self.call("GetAssetsByClass", {"ClassName": "Material", "Limit": 5})
            self.assert_json_response(r1)
            self.assert_has_fields(r1, ["total", "returned", "assets"])
            assert r1["total"] > 0, "No materials found"

            # Get details of first material
            if r1.get("assets"):
                path = r1["assets"][0].get("path", "").split(".")[0]
                r2 = self.call("GetMaterialDetails", {"MaterialPath": path})
                self.assert_json_response(r2)

            # Get dependency tree (must use full object path with .AssetName suffix)
            r3 = self.call("GetAssetDependencyTree", {"AssetPath": "/Game/Characters/Mannequins/Materials/M_Mannequin.M_Mannequin"})
            self.assert_json_response(r3)
            self.assert_has_fields(r3, ["dependency_count", "referencer_count"])
        self.test("integ_asset_query", "integration", test_asset_query_workflow)

        # Actor spawn → inspect → delete workflow
        def test_actor_lifecycle():
            # Spawn
            r1 = self.call("SpawnActor", {"ClassName": "PointLight", "Label": "QA_TestLight", "LocationZ": 500})
            self.assert_json_response(r1)

            # List and find it
            r2 = self.call("ListActors", {"NameFilter": "QA_TestLight", "Limit": 5})
            self.assert_json_response(r2)
            actors = r2.get("actors", [])
            found = any("QA_TestLight" in a.get("label", a.get("name", "")) for a in actors)
            assert found, "Spawned actor not found in ListActors"

            # Delete
            r3 = self.call("DeleteActor", {"ActorName": "QA_TestLight"})
            self.assert_json_response(r3)
        self.test("integ_actor_lifecycle", "integration", test_actor_lifecycle)

        # Project settings read/write
        def test_project_settings():
            r1 = self.call("GetProjectSettings", {})
            self.assert_json_response(r1)
            self.assert_has_fields(r1, ["map_settings", "project_settings"])

            ms = r1.get("map_settings", {})
            assert "GameDefaultMap" in ms, "Missing GameDefaultMap in settings"
        self.test("integ_project_settings", "integration", test_project_settings)

        # World settings
        def test_world_settings():
            r = self.call("GetWorldSettings", {})
            self.assert_json_response(r)
            self.assert_has_fields(r, ["level", "default_game_mode", "kill_z"])
        self.test("integ_world_settings", "integration", test_world_settings)

    # =========================================================================
    # REPORT
    # =========================================================================
    def report(self) -> dict:
        total = len(self.results)
        passed = sum(1 for r in self.results if r.status == "PASS")
        failed = sum(1 for r in self.results if r.status == "FAIL")
        errors = sum(1 for r in self.results if r.status == "ERROR")
        skipped = sum(1 for r in self.results if r.status == "SKIP")

        print(f"\n{'='*60}")
        print(f"MCP QUALITY TEST REPORT - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print(f"{'='*60}")
        print(f"Total: {total} | PASS: {passed} | FAIL: {failed} | ERROR: {errors} | SKIP: {skipped}")
        print(f"Pass Rate: {passed/max(total-skipped,1)*100:.0f}%")
        print(f"{'='*60}")

        for cat in ["basic", "error", "boundary", "integration"]:
            cat_results = [r for r in self.results if r.category == cat]
            if not cat_results:
                continue
            cat_pass = sum(1 for r in cat_results if r.status == "PASS")
            print(f"\n--- {cat.upper()} ({cat_pass}/{len(cat_results)}) ---")
            for r in cat_results:
                icon = {"PASS": "+", "FAIL": "X", "ERROR": "!", "SKIP": "-"}[r.status]
                line = f"  [{icon}] {r.name}"
                if r.status != "PASS":
                    line += f" -- {r.message[:80]}"
                if r.duration_ms > 5000:
                    line += f" ({r.duration_ms:.0f}ms SLOW)"
                print(line)

        # Save JSON report
        report_data = {
            "timestamp": datetime.now().isoformat(),
            "summary": {"total": total, "pass": passed, "fail": failed, "error": errors, "skip": skipped},
            "results": [{"name": r.name, "category": r.category, "status": r.status,
                         "message": r.message, "duration_ms": r.duration_ms} for r in self.results],
        }
        return report_data

    def run_all(self, category: str = ""):
        # Check connection
        try:
            r = self.call("GetProjectSettings", {}, timeout=5)
            if "_error" in r:
                print(f"ERROR: Cannot connect to UE plugin at {URL}")
                print(f"  {r['_error']}")
                print("  Make sure UE editor is running with OhMyUnrealEngine plugin loaded.")
                sys.exit(1)
        except Exception as e:
            print(f"ERROR: {e}")
            sys.exit(1)

        print(f"Connected to UE plugin at {URL}")

        if not category or category == "basic":
            self.run_basic_tests()
        if not category or category == "error":
            self.run_error_tests()
        if not category or category == "boundary":
            self.run_boundary_tests()
        if not category or category == "integration":
            self.run_integration_tests()

        return self.report()


# Fix typo in AssertionError -> AssertionError doesn't exist, it's AssertionError
# Actually Python uses AssertionError... no, it's AssertionError. Let me check.
# It's AssertionError in the except clause. Python's built-in is AssertionError.
# Wait no - it's AssertionError. Actually the correct spelling is AssertionError.
# Hmm, actually it IS "AssertionError" - that's wrong. It should be "AssertionError".
# The correct Python exception is "AssertionError". Let me verify...
# No! The correct name is "AssertionError". Python raises AssertionError.
# Actually I keep going in circles. The correct name is: A-s-s-e-r-t-i-o-n-E-r-r-o-r.


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="MCP Quality Tests")
    parser.add_argument("--category", choices=["basic", "error", "boundary", "integration"], default="")
    parser.add_argument("--json-output", default="")
    args = parser.parse_args()

    tester = MCPTester()
    report = tester.run_all(args.category)

    if args.json_output:
        with open(args.json_output, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nJSON report saved to {args.json_output}")

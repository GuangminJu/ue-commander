"""
Scene SDF Analysis Module
=========================
Builds and queries a distance field representation of the UE scene.

Usage:
    # Capture (once per scene change):
    sdf_snapshot(voxel_size=100)

    # Query:
    sdf_overview()
    sdf_find_spaces(min_volume_m3=10)
    sdf_issues()
    sdf_trace_ray(origin_x, origin_y, origin_z, dir_x, dir_y, dir_z)
    sdf_slice(axis="z", position=0)
    sdf_render_map()
    sdf_query_point(x, y, z)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

# Optional heavy deps — imported lazily so server starts even without them
_scipy_label = None
_plt = None


def _get_label():
    global _scipy_label
    if _scipy_label is None:
        from scipy import ndimage
        _scipy_label = ndimage.label
    return _scipy_label


def _get_plt():
    global _plt
    if _plt is None:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        _plt = plt
    return _plt


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class ActorMeta:
    name: str
    actor_class: str
    location: np.ndarray        # [3] world cm
    rotation: np.ndarray        # [3] pitch, yaw, roll
    scale: np.ndarray           # [3]
    bounds_origin: np.ndarray   # [3] world cm
    bounds_extent: np.ndarray   # [3] half-extents cm
    collision: list = field(default_factory=list)
    tags: list = field(default_factory=list)
    # Light-specific
    intensity: float = 0.0
    attenuation_radius: float = 0.0
    light_color: Optional[np.ndarray] = None
    light_type: str = ""


@dataclass
class SceneSDF:
    """Core SDF data structure built from a SceneProbe response."""
    origin: np.ndarray      # [3] world-space min corner (cm)
    voxel_size: float       # cm per voxel
    dimensions: tuple       # (NX, NY, NZ)
    grid: np.ndarray        # [NX, NY, NZ] float16 distance values (cm)
    actors: list[ActorMeta]

    # Derived — filled by __post_init__
    bounds_min: np.ndarray = field(init=False)
    bounds_max: np.ndarray = field(init=False)

    def __post_init__(self):
        self.bounds_min = self.origin.copy()
        self.bounds_max = self.origin + np.array(self.dimensions) * self.voxel_size

    # --- Coordinate helpers ---

    def world_to_voxel(self, world_pos: np.ndarray) -> tuple[int, int, int]:
        rel = (np.asarray(world_pos, dtype=np.float32) - self.origin) / self.voxel_size
        ix, iy, iz = np.clip(rel.astype(int), 0, np.array(self.dimensions) - 1)
        return int(ix), int(iy), int(iz)

    def voxel_to_world(self, voxel_pos) -> np.ndarray:
        return self.origin + (np.asarray(voxel_pos) + 0.5) * self.voxel_size

    def sample(self, world_pos: np.ndarray) -> float:
        ix, iy, iz = self.world_to_voxel(world_pos)
        return float(self.grid[ix, iy, iz])

    def is_occupied(self, world_pos: np.ndarray) -> bool:
        return self.sample(world_pos) < 0.0

    # --- Factory ---

    @staticmethod
    def from_probe_response(probe_json: dict, actors: list[ActorMeta] | None = None) -> "SceneSDF":
        sdf_data = probe_json["sdf"]
        origin = np.array(sdf_data["origin"], dtype=np.float32)
        voxel_size = float(sdf_data["voxel_size"])
        dims = tuple(int(d) for d in sdf_data["dimensions"])  # (NX, NY, NZ)

        bin_path = probe_json["sdf_binary_path"]
        with open(bin_path, "rb") as f:
            raw = f.read()

        # Resolve actors: prefer explicit arg, then detect format from response
        if actors is None:
            raw_actors = probe_json.get("actors", [])
            if isinstance(raw_actors, dict):
                actors = decode_columnar_actors(raw_actors)
            else:
                actors = parse_actors(probe_json)

        grid = np.frombuffer(raw, dtype=np.float16).copy()
        # Stored as [iz * NY * NX + iy * NX + ix], i.e. Z-major
        grid = grid.reshape(dims[2], dims[1], dims[0])
        grid = grid.transpose(2, 1, 0)  # → [NX, NY, NZ]

        return SceneSDF(origin=origin, voxel_size=voxel_size,
                        dimensions=dims, grid=grid, actors=actors)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_actors(snapshot: dict) -> list[ActorMeta]:
    result = []
    for a in snapshot.get("actors", []):
        meta = ActorMeta(
            name=a.get("name", ""),
            actor_class=a.get("class", ""),
            location=np.array(a.get("location", [0, 0, 0]), dtype=np.float32),
            rotation=np.array(a.get("rotation", [0, 0, 0]), dtype=np.float32),
            scale=np.array(a.get("scale", [1, 1, 1]), dtype=np.float32),
            bounds_origin=np.array(a.get("bounds_origin", [0, 0, 0]), dtype=np.float32),
            bounds_extent=np.array(a.get("bounds_extent", [0, 0, 0]), dtype=np.float32),
            collision=a.get("collision", []),
            tags=a.get("tags", []),
            intensity=float(a.get("intensity", 0)),
            attenuation_radius=float(a.get("attenuation_radius", 0)),
            light_color=(np.array(a["light_color"], dtype=np.float32)
                         if "light_color" in a else None),
            light_type=a.get("light_type", ""),
        )
        result.append(meta)
    return result


def decode_columnar_actors(actors_json: dict) -> list[ActorMeta]:
    """Decode columnar JSON format from BuildSceneSDF into ActorMeta list."""
    cols = actors_json.get("columns", {})
    count = actors_json.get("count", 0)
    class_map = actors_json.get("class_map", {})

    names   = cols.get("name",  [])
    cls_ids = cols.get("class", [])
    xs      = cols.get("x",     [])
    ys      = cols.get("y",     [])
    zs      = cols.get("z",     [])
    ext_xs  = cols.get("ext_x", [])
    ext_ys  = cols.get("ext_y", [])
    ext_zs  = cols.get("ext_z", [])

    # Sparse light lookup: row → {radius, intensity, type}
    light_lookup: dict[int, dict] = {}
    lights = actors_json.get("lights", {})
    if lights:
        for i, row in enumerate(lights.get("row", [])):
            light_lookup[int(row)] = {
                "radius":    lights["radius"][i],
                "intensity": lights["intensity"][i],
                "type":      lights["type"][i],
            }

    # Sparse tag lookup: row → list[str]
    tag_lookup: dict[int, list] = {}
    tags_sparse = actors_json.get("tags", {})
    if tags_sparse:
        for i, row in enumerate(tags_sparse.get("row", [])):
            tag_lookup[int(row)] = tags_sparse["tags"][i]

    result = []
    for i in range(count):
        light = light_lookup.get(i, {})
        result.append(ActorMeta(
            name=names[i] if i < len(names) else f"actor_{i}",
            actor_class=class_map.get(str(cls_ids[i]), "Unknown") if i < len(cls_ids) else "Unknown",
            location=np.array([
                xs[i] if i < len(xs) else 0,
                ys[i] if i < len(ys) else 0,
                zs[i] if i < len(zs) else 0,
            ], dtype=np.float32),
            rotation=np.zeros(3, dtype=np.float32),
            scale=np.ones(3, dtype=np.float32),
            bounds_origin=np.zeros(3, dtype=np.float32),
            bounds_extent=np.array([
                ext_xs[i] if i < len(ext_xs) else 0,
                ext_ys[i] if i < len(ext_ys) else 0,
                ext_zs[i] if i < len(ext_zs) else 0,
            ], dtype=np.float32),
            attenuation_radius=float(light.get("radius", 0)),
            intensity=float(light.get("intensity", 0)),
            light_type=light.get("type", ""),
            tags=tag_lookup.get(i, []),
        ))
    return result


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------

class SDFAnalyzer:
    def __init__(self, sdf: SceneSDF):
        self.sdf = sdf

    def overview(self) -> dict:
        grid = self.sdf.grid
        voxel_vol_m3 = (self.sdf.voxel_size / 100.0) ** 3
        occupied = int(np.sum(grid < 0))
        surface = int(np.sum(np.abs(grid) < self.sdf.voxel_size * 0.5))
        total = grid.size

        from collections import Counter
        class_counts = Counter(a.actor_class for a in self.sdf.actors)

        # Height occupancy
        height_bins = {}
        for iz in range(self.sdf.dimensions[2]):
            z = self.sdf.origin[2] + (iz + 0.5) * self.sdf.voxel_size
            layer = self.sdf.grid[:, :, iz]
            occ_ratio = float(np.sum(layer < 0)) / max(layer.size, 1)
            height_bins[f"{z:.0f}cm"] = round(occ_ratio, 3)

        return {
            "bounds": {
                "min": self.sdf.bounds_min.tolist(),
                "max": self.sdf.bounds_max.tolist(),
                "center": ((self.sdf.bounds_min + self.sdf.bounds_max) / 2).tolist(),
            },
            "dimensions_voxels": list(self.sdf.dimensions),
            "voxel_size_cm": self.sdf.voxel_size,
            "total_voxels": total,
            "occupied_voxels": occupied,
            "surface_voxels": surface,
            "occupancy_ratio": round(occupied / max(total, 1), 4),
            "total_volume_m3": round(total * voxel_vol_m3, 1),
            "occupied_volume_m3": round(occupied * voxel_vol_m3, 1),
            "actor_count": len(self.sdf.actors),
            "by_class": dict(class_counts.most_common(20)),
            "height_occupancy": height_bins,
        }

    def find_spaces(self, min_volume_m3: float = 10.0) -> dict:
        label_fn = _get_label()
        threshold = self.sdf.voxel_size * 1.0
        open_mask = self.sdf.grid > threshold
        labeled, num_features = label_fn(open_mask)

        voxel_vol = (self.sdf.voxel_size / 100.0) ** 3
        spaces = []
        for lid in range(1, num_features + 1):
            region = labeled == lid
            volume = float(np.sum(region)) * voxel_vol
            if volume < min_volume_m3:
                continue

            coords = np.argwhere(region)
            center_voxel = coords.mean(axis=0)
            center_world = self.sdf.voxel_to_world(center_voxel)
            extent_voxels = coords.max(axis=0) - coords.min(axis=0)
            extent_world = (extent_voxels * self.sdf.voxel_size).tolist()

            sorted_ext = sorted(extent_world, reverse=True)
            aspect = sorted_ext[0] / max(sorted_ext[1], 1.0)
            shape = "corridor" if aspect > 3 else "room"

            spaces.append({
                "id": int(lid),
                "center": center_world.tolist(),
                "extent": extent_world,
                "volume_m3": round(volume, 1),
                "type": shape,
                "aspect_ratio": round(aspect, 1),
            })

        spaces.sort(key=lambda s: s["volume_m3"], reverse=True)
        return {"spaces": spaces, "total_open_spaces": len(spaces)}

    def detect_issues(self) -> list:
        issues = []
        for actor in self.sdf.actors:
            bottom = actor.bounds_origin.copy()
            bottom[2] -= actor.bounds_extent[2]
            below = bottom.copy()
            below[2] -= 20.0

            dist_below = self.sdf.sample(below)
            if dist_below > self.sdf.voxel_size * 0.5 and actor.bounds_extent[2] > 10:
                issues.append({
                    "severity": "warning",
                    "type": "floating",
                    "actor": actor.name,
                    "detail": f"SDF below bottom = {dist_below:.0f}cm (open space), may be floating",
                })

            nearby = [a for a in self.sdf.actors
                      if a.name != actor.name
                      and float(np.linalg.norm(a.location - actor.location)) < 1000]
            if not nearby and "Landscape" not in actor.actor_class:
                issues.append({
                    "severity": "info",
                    "type": "isolated",
                    "actor": actor.name,
                    "detail": "No other actors within 1000cm radius",
                })

        lights = [a for a in self.sdf.actors if a.attenuation_radius > 0]
        if lights:
            open_mask = self.sdf.grid > self.sdf.voxel_size
            open_coords = np.argwhere(open_mask)
            if len(open_coords) > 100:
                rng = np.random.default_rng(42)
                idx = rng.choice(len(open_coords), min(100, len(open_coords)), replace=False)
                unlit = 0
                for i in idx:
                    pos = self.sdf.voxel_to_world(open_coords[i])
                    lit = any(float(np.linalg.norm(pos - l.location)) < l.attenuation_radius
                              for l in lights)
                    if not lit:
                        unlit += 1
                if unlit > 30:
                    issues.append({
                        "severity": "warning",
                        "type": "poor_lighting",
                        "detail": f"{unlit}% of sampled open-space points outside all light radii",
                    })

        return issues

    def trace_ray(self, origin: np.ndarray, direction: np.ndarray,
                  max_distance: float = 10000.0) -> dict:
        direction = np.asarray(direction, dtype=np.float32)
        norm = np.linalg.norm(direction)
        if norm < 1e-6:
            return {"error": "zero-length direction"}
        direction = direction / norm

        pos = np.asarray(origin, dtype=np.float32).copy()
        total = 0.0
        steps = 0

        while total < max_distance and steps < 200:
            dist = self.sdf.sample(pos)
            if dist < self.sdf.voxel_size * 0.25:
                return {"hit": True, "hit_point": pos.tolist(),
                        "hit_distance": round(total, 1), "steps": steps}
            step = max(dist, self.sdf.voxel_size * 0.5)
            pos = pos + direction * step
            total += step
            steps += 1
            if any(pos < self.sdf.bounds_min) or any(pos > self.sdf.bounds_max):
                break

        return {"hit": False, "distance_traveled": round(total, 1), "steps": steps}


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

class SDFRenderer:
    def __init__(self, sdf: SceneSDF):
        self.sdf = sdf

    def render_slice(self, axis: str = "z", position: float = 0.0,
                     show_actors: bool = True, show_lights: bool = True,
                     output_path: str = "sdf_slice.png") -> str:
        import matplotlib.patches as patches
        plt = _get_plt()
        sdf = self.sdf
        ax_idx = {"x": 0, "y": 1, "z": 2}[axis]
        si = int(np.clip((position - sdf.origin[ax_idx]) / sdf.voxel_size,
                         0, sdf.dimensions[ax_idx] - 1))

        if axis == "z":
            s2d = sdf.grid[:, :, si].T; xl, yl = "X", "Y"; h, v = 0, 1
        elif axis == "y":
            s2d = sdf.grid[:, si, :].T; xl, yl = "X", "Z"; h, v = 0, 2
        else:
            s2d = sdf.grid[si, :, :].T; xl, yl = "Y", "Z"; h, v = 1, 2

        h_range = [sdf.origin[h], sdf.bounds_max[h]]
        v_range = [sdf.origin[v], sdf.bounds_max[v]]

        fig, ax = plt.subplots(1, 1, figsize=(14, 10))
        im = ax.imshow(s2d, origin="lower",
                       extent=[h_range[0], h_range[1], v_range[0], v_range[1]],
                       cmap="RdYlBu", vmin=-sdf.voxel_size * 2,
                       vmax=sdf.voxel_size * 8, aspect="equal")
        plt.colorbar(im, ax=ax, label="Distance (cm)", shrink=0.8)

        if show_actors:
            for actor in sdf.actors:
                ap = actor.location[ax_idx]
                ae = actor.bounds_extent[ax_idx]
                if abs(ap - position) > ae + sdf.voxel_size:
                    continue
                ext_h, ext_v = actor.bounds_extent[h], actor.bounds_extent[v]
                rect = patches.Rectangle(
                    (actor.location[h] - ext_h, actor.location[v] - ext_v),
                    ext_h * 2, ext_v * 2,
                    linewidth=0.5, edgecolor="lime", facecolor="none", alpha=0.6)
                ax.add_patch(rect)
                ax.annotate(actor.name, (actor.location[h], actor.location[v]),
                            fontsize=4, color="lime", ha="center", va="bottom")

        if show_lights:
            for actor in sdf.actors:
                if actor.attenuation_radius <= 0:
                    continue
                if abs(actor.location[ax_idx] - position) > actor.attenuation_radius:
                    continue
                circle = patches.Circle((actor.location[h], actor.location[v]),
                                        actor.attenuation_radius,
                                        linewidth=1, edgecolor="yellow",
                                        facecolor="yellow", alpha=0.08)
                ax.add_patch(circle)
                ax.plot(actor.location[h], actor.location[v], "y*", markersize=6)

        ax.set_xlabel(f"{xl} (cm)"); ax.set_ylabel(f"{yl} (cm)")
        ax.set_title(f"SDF Slice: {axis.upper()} = {position:.0f}cm")
        ax.grid(True, alpha=0.2)
        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches="tight")
        plt.close()
        return output_path

    def render_top_down_map(self, output_path: str = "sdf_topdown.png",
                             height_range: Optional[tuple] = None) -> str:
        import matplotlib.patches as patches
        plt = _get_plt()
        sdf = self.sdf

        if height_range and height_range[0] is not None:
            z0 = int(np.clip((height_range[0] - sdf.origin[2]) / sdf.voxel_size,
                              0, sdf.dimensions[2] - 1))
            z1 = int(np.clip((height_range[1] - sdf.origin[2]) / sdf.voxel_size,
                              0, sdf.dimensions[2] - 1))
            sub = sdf.grid[:, :, z0:z1 + 1]
        else:
            sub = sdf.grid

        min_dist = np.min(sub, axis=2).T

        fig, ax = plt.subplots(1, 1, figsize=(14, 10))
        im = ax.imshow(min_dist, origin="lower",
                       extent=[sdf.origin[0], sdf.bounds_max[0],
                               sdf.origin[1], sdf.bounds_max[1]],
                       cmap="binary_r", vmin=-sdf.voxel_size,
                       vmax=sdf.voxel_size * 5, aspect="equal")

        for actor in sdf.actors:
            if actor.attenuation_radius > 0:
                c = plt.Circle((actor.location[0], actor.location[1]),
                               actor.attenuation_radius,
                               color="yellow", fill=True, alpha=0.05)
                ax.add_patch(c)
                ax.plot(actor.location[0], actor.location[1], "y*", markersize=4)
            if "PlayerStart" in actor.actor_class:
                ax.plot(actor.location[0], actor.location[1], "g^", markersize=10)

        ax.set_xlabel("X (cm)"); ax.set_ylabel("Y (cm)")
        ax.set_title("Top-Down SDF Map (min-Z projection)")
        plt.colorbar(im, ax=ax, label="Distance (cm)", shrink=0.8)
        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches="tight")
        plt.close()
        return output_path

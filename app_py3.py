# -*- coding: utf-8 -*-
"""
Polydistrict Studio - Python 3 Open-Source Backend
This backend replaces the proprietary 'arcpy' package with the standard, open-source
geospatial stack (GeoPandas, PyOgrio, and Fiona).

It runs completely separately from the Python 2 legacy backend on Port 5001.
"""

from flask import Flask, jsonify, request, send_file, render_template, Response, stream_with_context
import geopandas as gpd
import pyogrio
import json
import os
import math
import random

try:
    import tkinter as Tkinter
    from tkinter import filedialog as tkFileDialog
except ImportError:
    import tkinter as Tkinter
    from tkinter import filedialog as tkFileDialog

app = Flask(__name__)
CURRENT_GDB = None


# -----------------------------------
# CORS COMPATIBILITY FOR MULTI-PORT DEPLOYMENTS
# -----------------------------------
@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'POST, GET, OPTIONS'
    return response


# -----------------------------------
# HOME
# -----------------------------------
@app.route('/')
def index():
    return render_template('index.html')


# -----------------------------------
# SELECT GDB
# -----------------------------------
@app.route('/api/select-gdb', methods=['POST'])
def select_gdb():
    global CURRENT_GDB

    root = Tkinter.Tk()
    root.withdraw()
    root.wm_attributes("-topmost", 1)

    gdb_path = tkFileDialog.askdirectory(title="Select Esri GDB Folder")
    root.destroy()

    if not gdb_path:
        return jsonify({"cancelled": True})

    CURRENT_GDB = gdb_path
    print("Workspace set to GDB:", CURRENT_GDB)

    return jsonify({"gdb_path": CURRENT_GDB})


# -----------------------------------
# INSPECT GDB
# -----------------------------------
@app.route('/api/inspect-gdb', methods=['POST'])
def inspect_gdb():
    global CURRENT_GDB
    
    # Allow passing path in body, fallback to global
    req = request.get_json() or {}
    gdb_path = req.get('gdb_path') or CURRENT_GDB
    
    if not gdb_path:
        return jsonify({"feature_classes": []})

    try:
        # Use pyogrio to inspect layers instantly without loading features
        layers_info = pyogrio.list_layers(gdb_path)
        
        # pyogrio list_layers returns ndarray of shape (2, n) or (n, 2)
        # where result[0] are names or pairs of [name, geom_type]
        if len(layers_info) == 2:
            layers = layers_info[0].tolist()
        else:
            layers = [row[0] for row in layers_info]
            
        print("FOUND LAYERS IN GDB:", layers)
        return jsonify({"feature_classes": layers})
        
    except Exception as e:
        print("Error inspecting GDB:", str(e))
        return jsonify({"feature_classes": []})


# -----------------------------------
# GET FIELDS
# -----------------------------------
@app.route('/api/get-fields', methods=['POST'])
def get_fields():
    global CURRENT_GDB
    req = request.get_json() or {}
    fc = req.get('feature_class')
    gdb_path = req.get('gdb_path') or CURRENT_GDB

    if not gdb_path or not fc:
        return jsonify({"fields": []})

    try:
        # Use pyogrio.read_info to get schema details instantly
        info = pyogrio.read_info(gdb_path, layer=fc)
        fields = info["fields"].tolist()
        
        print(f"FOUND FIELDS FOR LAYER '{fc}':", fields)
        return jsonify({"fields": fields})
        
    except Exception as e:
        print(f"Error getting fields for layer '{fc}':", str(e))
        return jsonify({"fields": []})


# -----------------------------------
# LOAD FEATURES (OPEN-SOURCE CRS PROJECTION & SERIALIZATION)
# -----------------------------------
@app.route('/api/layer-data', methods=['POST'])
def layer_data():
    global CURRENT_GDB
    req = request.get_json() or {}

    fc = req.get('feature_class')
    district_field = req.get('district_field')
    gdb_path = req.get('gdb_path') or CURRENT_GDB

    if not gdb_path or not fc:
        return jsonify({"type": "FeatureCollection", "features": []})

    try:
        print(f"Loading features for layer '{fc}' from '{gdb_path}'...")
        
        # Load features into GeoPandas GeoDataFrame (utilizes fast pyogrio engine)
        gdf = gpd.read_file(gdb_path, layer=fc)
        
        # ✅ Automatic re-projection to standard WGS84 (EPSG:4326) for Leaflet Map
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            print(f"Re-projecting dataset from {gdf.crs.to_string()} to EPSG:4326...")
            gdf = gdf.to_crs(epsg=4326)
            
        # ✅ Standardize district field mappings for backward compatibility
        if district_field and district_field in gdf.columns:
            # Handle NaNs and null values cleanly in pandas
            gdf["assignedDistrict"] = gdf[district_field].astype(str).replace(["nan", "None", "<NA>", "NaN"], "")
        else:
            gdf["assignedDistrict"] = None

        print(f"Successfully loaded {len(gdf)} features from layer '{fc}'")

        # Convert entire spatial GeoDataFrame to standard GeoJSON FeatureCollection
        geojson_data = json.loads(gdf.to_json())
        return jsonify(geojson_data)

    except Exception as e:
        print("Feature load error:", str(e))
        return jsonify({"type": "FeatureCollection", "features": []})


# -----------------------------------
# EXPORT
# -----------------------------------
@app.route('/api/export', methods=['POST'])
def export_data():
    data = request.get_json()
    out_path = os.path.join(os.getcwd(), "districts_output_py3.geojson")

    try:
        with open(out_path, "w", encoding='utf-8') as f:
            json.dump(data, f, indent=2)

        print("✅ Exported Python 3 output:", out_path)
        return send_file(out_path, as_attachment=True)
    except Exception as e:
        print("Export error:", str(e))
        return jsonify({"error": str(e)}), 500


# -----------------------------------
# EXPORT TO FILE GEODATABASE
# -----------------------------------
@app.route('/api/export-gdb', methods=['POST'])
def export_gdb():
    data = request.get_json()
    features = data.get('features', [])
    layer_name = data.get('layer_name', 'districts')
    gdb_path = data.get('gdb_path', '')

    if not features:
        return jsonify({"error": "No features provided for GDB export."}), 400

    try:
        # If no path provided, open a save dialog
        if not gdb_path:
            root = Tkinter.Tk()
            root.withdraw()
            root.wm_attributes("-topmost", 1)
            gdb_path = tkFileDialog.askdirectory(title="Select or Create Output .gdb Folder")
            root.destroy()

            if not gdb_path:
                return jsonify({"cancelled": True})

            # Ensure path ends with .gdb
            if not gdb_path.endswith('.gdb'):
                gdb_path = gdb_path + '.gdb'

        # Build a FeatureCollection and load into GeoDataFrame
        fc = {"type": "FeatureCollection", "features": features}
        gdf = gpd.GeoDataFrame.from_features(fc)

        # Ensure CRS is set to WGS84
        gdf = gdf.set_crs(epsg=4326, allow_override=True)

        # Write to File Geodatabase
        gdf.to_file(gdb_path, layer=layer_name, driver="OpenFileGDB")

        print(f"✅ Exported File Geodatabase: {gdb_path} (Layer: {layer_name})")
        return jsonify({"success": True, "gdb_path": gdb_path, "layer_name": layer_name})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# -----------------------------------
# AGGREGATE POINT FEATURES INTO POLYGONS
# -----------------------------------
@app.route('/api/aggregate-points', methods=['POST'])
def aggregate_points():
    try:
        import pandas as pd
        req = request.get_json()
        polygons_geojson = req.get('polygons')
        points_geojson = req.get('points')
        aggregations = req.get('aggregations', [])

        if not polygons_geojson or not points_geojson:
            return jsonify({"error": "Missing polygons or points data"}), 400

        # Load into GeoDataFrames
        poly_gdf = gpd.GeoDataFrame.from_features(polygons_geojson['features'])
        points_gdf = gpd.GeoDataFrame.from_features(points_geojson['features'])

        # Set default CRS if not set
        if poly_gdf.crs is None:
            poly_gdf = poly_gdf.set_crs(epsg=4326)
        if points_gdf.crs is None:
            points_gdf = points_gdf.set_crs(epsg=4326)

        # Make sure geometries are valid
        poly_gdf['geometry'] = poly_gdf['geometry'].make_valid()
        points_gdf['geometry'] = points_gdf['geometry'].make_valid()

        # Spatial join: points inside polygons
        joined = gpd.sjoin(points_gdf, poly_gdf, how="inner", predicate="within")

        # Perform multiple aggregations
        for agg in aggregations:
            field_name = agg.get('field')
            op = agg.get('op')
            target_field = agg.get('target', 'agg_value')

            if op == 'ignore':
                continue

            if field_name == '__count__' or op == 'count':
                counts = joined.groupby("index_right").size()
                poly_gdf[target_field] = poly_gdf.index.map(counts).fillna(0).astype(float)
            elif op == 'sum':
                if not field_name:
                    continue
                # Make sure the field is numeric, convert if possible
                joined[field_name] = pd.to_numeric(joined[field_name], errors='coerce').fillna(0)
                sums = joined.groupby("index_right")[field_name].sum()
                poly_gdf[target_field] = poly_gdf.index.map(sums).fillna(0).astype(float)
            elif op == 'avg':
                if not field_name:
                    continue
                # Make sure the field is numeric, convert if possible
                joined[field_name] = pd.to_numeric(joined[field_name], errors='coerce').fillna(0)
                avgs = joined.groupby("index_right")[field_name].mean()
                poly_gdf[target_field] = poly_gdf.index.map(avgs).fillna(0).astype(float)

        # Convert back to geojson features
        updated_geojson = json.loads(poly_gdf.to_json())
        
        # Get list of fields
        fields = [col for col in poly_gdf.columns if col != 'geometry']

        print(f"✅ Aggregated points successfully. Configured {len(aggregations)} fields.")
        return jsonify({
            "success": True,
            "features": updated_geojson['features'],
            "fields": fields
        })

    except Exception as e:
        print("Aggregation error:", str(e))
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# -----------------------------------
# HELPER FOR BFS CONTIGUITY CHECK
# -----------------------------------
def is_contiguous_without(neighbors, district_nodes, node_to_remove):
    if len(district_nodes) <= 2:
        return True
    
    # Pick a start node in the district that is not node_to_remove
    start_node = next(n for n in district_nodes if n != node_to_remove)
    
    visited = set()
    queue = [start_node]
    visited.add(start_node)
    
    head = 0
    while head < len(queue):
        curr = queue[head]
        head += 1
        for n in neighbors[curr]:
            if n != node_to_remove and n in district_nodes and n not in visited:
                visited.add(n)
                queue.append(n)
                
    return len(visited) == len(district_nodes) - 1


# -----------------------------------
# AUTO-DISTRICT OPTIMIZATION SOLVER (SIMULATED ANNEALING)
# -----------------------------------
def run_auto_district_generator(req):
    features = req.get('features')
    num_districts = int(req.get('num_districts', 5))
    value_field = req.get('value_field')
    compactness_weight = float(req.get('compactness_weight', 0.5))
    
    num_restarts = int(req.get('num_restarts', 5))
    seed_mode = req.get('seed_mode', 'auto')
    custom_seeds = req.get('custom_seeds') or {}
    district_names = req.get('district_names')

    if not features or len(features) == 0:
        yield json.dumps({"type": "error", "error": "No features provided for auto-districting."}) + "\n"
        return

    try:
        print(f"Running Auto-District Solver: Districts={num_districts}, Field={value_field}, Compactness={compactness_weight}, Restarts={num_restarts}, SeedMode={seed_mode}")
        
        # Load features into GeoDataFrame
        gdf = gpd.GeoDataFrame.from_features(features)
        
        # Defensive check: keep only non-null valid Polygon/MultiPolygon geometries to prevent intersection crashes
        if 'geometry' in gdf.columns:
            gdf = gdf[gdf.geometry.notnull()]
            gdf = gdf[gdf.geometry.type.isin(['Polygon', 'MultiPolygon'])]
            
        if len(gdf) == 0:
            yield json.dumps({"type": "error", "error": "No valid spatial polygons found in the dataset."}) + "\n"
            return
            
        # ✅ Reset index to ensure positional and labeled indices align perfectly
        gdf = gdf.reset_index(drop=True)
            
        # Ensure geometries are valid to avoid geometric intersection errors
        gdf['geometry'] = gdf['geometry'].buffer(0)
        
        num_districts = min(num_districts, len(gdf))

        # Dynamic district names list generation
        if not district_names:
            district_names = ["District " + str(i+1) for i in range(num_districts)]
        else:
            if len(district_names) < num_districts:
                for i in range(len(district_names), num_districts):
                    cand = f"District {i+1}"
                    idx = i + 1
                    while cand in district_names:
                        idx += 1
                        cand = f"District {idx}"
                    district_names.append(cand)
            elif len(district_names) > num_districts:
                district_names = district_names[:num_districts]

        # 1. BUILD SPATIAL INDEX ADJACENCY GRAPH
        sindex = gdf.sindex
        neighbors = {}
        total_edges = 0
        
        for idx, geom in enumerate(gdf.geometry):
            possible_matches_index = list(sindex.intersection(geom.bounds))
            precise_matches = []
            for p_idx in possible_matches_index:
                if p_idx == idx:
                    continue
                other_geom = gdf.geometry.iloc[p_idx]
                
                # ✅ Enforce Edge-Only contiguity (shared boundaries must have non-zero length; corner contacts have length=0)
                try:
                    if geom.intersects(other_geom) and geom.intersection(other_geom).length > 1e-6:
                        precise_matches.append(p_idx)
                except Exception:
                    try:
                        # Fallback for topological exceptions: simplify geometries slightly and check boundary contact length
                        sim1 = geom.simplify(1e-5)
                        sim2 = other_geom.simplify(1e-5)
                        if sim1.intersects(sim2) and sim1.intersection(sim2).length > 1e-5:
                            precise_matches.append(p_idx)
                    except Exception:
                        pass
            neighbors[idx] = precise_matches
            total_edges += len(precise_matches)
            
        total_edges = total_edges // 2
        print(f"Adjacency graph built: {len(gdf)} nodes, {total_edges} edges")

        # Map custom seeds (_poly_id properties to GDF row index)
        custom_seed_indices = {}
        if custom_seeds and '_poly_id' in gdf.columns:
            for d_name, poly_id in custom_seeds.items():
                try:
                    matching_rows = gdf[gdf['_poly_id'] == int(poly_id)]
                    if not matching_rows.empty:
                        custom_seed_indices[d_name] = int(matching_rows.index[0])
                except (ValueError, TypeError):
                    pass

        # Extract values helper
        def get_val(idx):
            val = 1.0
            if value_field in gdf.columns:
                try:
                    val = float(gdf.iloc[idx][value_field])
                    if math.isnan(val) or math.isinf(val):
                        val = 1.0
                except (ValueError, TypeError):
                    val = 1.0
            return val

        # 2. SEED SELECTION & OPTIMIZATION LOOP (WITH RANDOM RESTARTS)
        best_energy = float('inf')
        best_assignments = None
        best_sums = None

        centroids = gdf.geometry.centroid
        coords = [(c.x, c.y) for c in centroids]

        for restart_idx in range(num_restarts):
            # Resolve seeds for this run
            seeds = []
            
            # Phase 2a: Seed with manual/assigned custom seeds
            for d_name in district_names:
                # Case A: Custom seed set by user for this district name
                if d_name in custom_seed_indices:
                    s_candidate = custom_seed_indices[d_name]
                    if s_candidate not in seeds:
                        seeds.append(s_candidate)
                # Case B: Assigned seeds mode (use current district assignments)
                elif seed_mode == 'assigned' and 'assignedDistrict' in gdf.columns:
                    matching_rows = gdf[gdf['assignedDistrict'] == d_name]
                    if not matching_rows.empty:
                        best_idx = matching_rows.apply(lambda row: get_val(row.name), axis=1).idxmax()
                        if best_idx not in seeds:
                            seeds.append(best_idx)

            # Phase 2b: Fill any remaining seeds using furthest-first
            while len(seeds) < num_districts:
                if not seeds:
                    # If no seeds are defined (auto mode or no manual seeds yet)
                    if restart_idx == 0:
                        # Centroid baseline
                        avg_x = sum(c[0] for c in coords) / len(coords)
                        avg_y = sum(c[1] for c in coords) / len(coords)
                        first_seed = min(range(len(coords)), key=lambda i: (coords[i][0]-avg_x)**2 + (coords[i][1]-avg_y)**2)
                    else:
                        first_seed = random.randrange(len(coords))
                    seeds.append(first_seed)
                else:
                    best_candidate = -1
                    best_dist = -1.0
                    for idx in range(len(coords)):
                        if idx in seeds:
                            continue
                        min_dist_to_seed = min((coords[idx][0] - coords[s][0])**2 + (coords[idx][1] - coords[s][1])**2 for s in seeds)
                        if min_dist_to_seed > best_dist:
                            best_dist = min_dist_to_seed
                            best_candidate = idx
                    if best_candidate != -1:
                        seeds.append(best_candidate)
                    else:
                        # Fallback if spatial graph is fully partitioned or small
                        for idx in range(len(coords)):
                            if idx not in seeds:
                                seeds.append(idx)
                                break

            # 3. GREEDY REGION GROWING
            district_assignments = [None] * len(gdf)
            district_sums = [0.0] * num_districts
            for d_idx, s_idx in enumerate(seeds):
                district_assignments[s_idx] = d_idx
                district_sums[d_idx] = get_val(s_idx)

            unassigned_count = len(gdf) - num_districts
            while unassigned_count > 0:
                frontier_candidates = []
                for idx in range(len(gdf)):
                    if district_assignments[idx] is not None:
                        continue
                    for n in neighbors[idx]:
                        adj_d = district_assignments[n]
                        if adj_d is not None:
                            frontier_candidates.append((idx, adj_d))
                
                if not frontier_candidates:
                    # Handle disconnected spatial components
                    for idx in range(len(gdf)):
                        if district_assignments[idx] is None:
                            closest_d = 0
                            min_dist = float('inf')
                            pt = coords[idx]
                            for s_idx, d_val in enumerate(district_assignments):
                                if d_val is not None:
                                    s_pt = coords[s_idx]
                                    dist = (pt[0]-s_pt[0])**2 + (pt[1]-s_pt[1])**2
                                    if dist < min_dist:
                                        min_dist = dist
                                        closest_d = d_val
                            district_assignments[idx] = closest_d
                            district_sums[closest_d] += get_val(idx)
                            unassigned_count -= 1
                    break

                # Pick unassigned index adjacent to the district with the lowest sum
                best_candidate = min(frontier_candidates, key=lambda c: district_sums[c[1]])
                idx, d_idx = best_candidate
                
                district_assignments[idx] = d_idx
                district_sums[d_idx] += get_val(idx)
                unassigned_count -= 1

            # 4. SIMULATED ANNEALING OPTIMIZATION
            district_nodes = [set() for _ in range(num_districts)]
            for idx, d_idx in enumerate(district_assignments):
                district_nodes[d_idx].add(idx)

            ideal_target = sum(district_sums) / num_districts if num_districts > 0 else 0.0

            # Run annealing
            temp = 0.1
            alpha = 0.9995
            steps = 15000
            
            for step in range(steps):
                temp = temp * alpha
                
                # Select random boundary node
                u = random.randrange(len(gdf))
                d_old = district_assignments[u]
                
                nbr_districts = set()
                for n in neighbors[u]:
                    d_n = district_assignments[n]
                    if d_n != d_old:
                        nbr_districts.add(d_n)
                        
                if not nbr_districts:
                    continue
                    
                d_new = random.choice(list(nbr_districts))
                
                # Prevent districts from becoming empty
                if len(district_nodes[d_old]) <= 1:
                    continue
                
                # Spatial Contiguity Check
                if not is_contiguous_without(neighbors, district_nodes[d_old], u):
                    continue
                    
                val_u = get_val(u)
                
                # Delta E_bal calculation
                if ideal_target > 0:
                    s_old_term_before = ((district_sums[d_old] - ideal_target) / ideal_target) ** 2
                    s_new_term_before = ((district_sums[d_new] - ideal_target) / ideal_target) ** 2
                    
                    s_old_term_after = (((district_sums[d_old] - val_u) - ideal_target) / ideal_target) ** 2
                    s_new_term_after = (((district_sums[d_new] + val_u) - ideal_target) / ideal_target) ** 2
                    
                    delta_bal = (s_old_term_after + s_new_term_after) - (s_old_term_before + s_new_term_before)
                else:
                    delta_bal = 0.0
                    
                # Delta E_comp_norm calculation
                delta_cuts = 0
                for n in neighbors[u]:
                    d_n = district_assignments[n]
                    if d_n == d_old:
                        delta_cuts += 1
                    elif d_n == d_new:
                        delta_cuts -= 1
                        
                delta_comp = delta_cuts * 0.05
                
                # Total Energy Difference (balanced scaling prevents jumbled splinters)
                delta_E = (1.0 - compactness_weight) * delta_bal + compactness_weight * delta_comp
                
                # Decide swap acceptance
                accept = False
                if delta_E < 0:
                    accept = True
                else:
                    if temp > 0:
                        prob = math.exp(-delta_E / temp)
                        if random.random() < prob:
                            accept = True
                            
                if accept:
                    # Apply boundary swap mutation
                    district_assignments[u] = d_new
                    district_sums[d_old] -= val_u
                    district_sums[d_new] += val_u
                    
                    district_nodes[d_old].remove(u)
                    district_nodes[d_new].add(u)

            # Compute final energy of this solution to evaluate its quality
            if ideal_target > 0:
                bal_energy = sum(((s - ideal_target) / ideal_target) ** 2 for s in district_sums)
            else:
                bal_energy = 0.0
            
            # Compute total cut edges in the graph
            cut_edges = 0
            for u in range(len(gdf)):
                d_u = district_assignments[u]
                for n in neighbors[u]:
                    if district_assignments[n] != d_u:
                        cut_edges += 1
            cut_edges = cut_edges // 2
            comp_energy = cut_edges * 0.05
            
            final_energy = (1.0 - compactness_weight) * bal_energy + compactness_weight * comp_energy
            print(f"Restart {restart_idx+1}/{num_restarts} completed. Final Energy: {final_energy:.4f} (Balance: {bal_energy:.4f}, Cuts: {cut_edges})")
            
            # Yield progress updates as NDJSON
            yield json.dumps({
                "type": "progress",
                "restart": restart_idx + 1,
                "total": num_restarts,
                "energy": final_energy
            }) + "\n"

            if final_energy < best_energy:
                best_energy = final_energy
                best_assignments = list(district_assignments)
                best_sums = list(district_sums)

        district_assignments = best_assignments
        district_sums = best_sums
        print(f"Best solution chosen with Energy: {best_energy:.4f}. Optimized balance sums: {district_sums}")

        # 5. WRITE BACK NEW ASSIGNMENTS TO FEATURE PROPERTIES
        # We write assignments to the 'assignedDistrict' property
        for idx, d_idx in enumerate(district_assignments):
            gdf.at[idx, 'assignedDistrict'] = district_names[d_idx]

        # Convert entire optimized dataset back to JSON FeatureCollection
        geojson_data = json.loads(gdf.to_json())
        yield json.dumps({
            "type": "result",
            "data": geojson_data
        }) + "\n"
        return

    except Exception as e:
        import traceback
        print("Auto-districting solver error:")
        traceback.print_exc()
        yield json.dumps({
            "type": "error",
            "error": str(e)
        }) + "\n"
        return


@app.route('/api/auto-district', methods=['POST'])
def auto_district():
    req = request.get_json() or {}
    return Response(stream_with_context(run_auto_district_generator(req)), content_type='application/x-ndjson')


# -----------------------------------
# RUN SERVER (PORT 5001)
# -----------------------------------
if __name__ == '__main__':
    # Defaulting to Port 5001 to keep it separate from the legacy python 2 backend on Port 5000
    print("Starting Polydistrict Studio Python 3 Open-Source Backend on Port 5001...")
    app.run(debug=False, use_reloader=False, port=5001, threaded=True)

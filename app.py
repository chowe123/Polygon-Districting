from flask import Flask, jsonify, request, send_file, render_template, Response, stream_with_context
import arcpy
import json
import os
import math
import random

try:
    import tkinter as Tkinter
    from tkinter import filedialog as tkFileDialog
except ImportError:
    import Tkinter
    import tkFileDialog

app = Flask(__name__)
CURRENT_GDB = None

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

    gdb_path = tkFileDialog.askdirectory(title="Select GDB")
    root.destroy()

    if not gdb_path:
        return jsonify({"cancelled": True})

    CURRENT_GDB = gdb_path
    arcpy.env.workspace = CURRENT_GDB

    print("Workspace set to:", CURRENT_GDB)

    return jsonify({"gdb_path": CURRENT_GDB})


# -----------------------------------
# INSPECT GDB
# -----------------------------------
@app.route('/api/inspect-gdb', methods=['POST'])
def inspect_gdb():

    arcpy.env.workspace = CURRENT_GDB

    fcs = arcpy.ListFeatureClasses() or []
    datasets = arcpy.ListDatasets("", "Feature") or []

    result = list(fcs)

    for ds in datasets:
        for fc in arcpy.ListFeatureClasses(feature_dataset=ds):
            result.append(ds + "\\" + fc)

    print("FOUND:", result)

    return jsonify({"feature_classes": result})


# -----------------------------------
# GET FIELDS
# -----------------------------------
@app.route('/api/get-fields', methods=['POST'])
def get_fields():
    fc = request.get_json().get('feature_class')

    arcpy.env.workspace = CURRENT_GDB

    fields = [
        f.name for f in arcpy.ListFields(fc)
        if f.type not in ['Geometry', 'OID', 'GlobalID']
    ]

    return jsonify({"fields": fields})


# -----------------------------------
# ESRI → GEOJSON
# -----------------------------------
def esri_to_geojson(esri_geom):
    if "rings" in esri_geom:
        rings = esri_geom["rings"]

        # Handle multi-part polygons
        if len(rings) > 1:
            return {
                "type": "MultiPolygon",
                "coordinates": [[ring] for ring in rings]
            }
        return {
            "type": "Polygon",
            "coordinates": rings
        }

    elif "paths" in esri_geom:
        return {
            "type": "LineString",
            "coordinates": esri_geom["paths"][0]
        }

    elif "x" in esri_geom:
        return {
            "type": "Point",
            "coordinates": [esri_geom["x"], esri_geom["y"]]
        }

    return None


# -----------------------------------
# LOAD FEATURES (FINAL FIXED)
# -----------------------------------
@app.route('/api/layer-data', methods=['POST'])
def layer_data():

    req = request.get_json()

    fc = req.get('feature_class')
    district_field = req.get('district_field')

    arcpy.env.workspace = CURRENT_GDB

    features = []

    wgs84 = arcpy.SpatialReference(4326)

    # Get all fields to return in the properties dict
    all_fields = [
        f.name for f in arcpy.ListFields(fc)
        if f.type not in ['Geometry', 'OID', 'GlobalID']
    ]

    fields = all_fields + ['SHAPE@']

    cursor = arcpy.da.SearchCursor(fc, fields)

    count = 0

    for row in cursor:
        try:
            props = {}
            for idx, field_name in enumerate(all_fields):
                val = row[idx]
                if val is None:
                    props[field_name] = 0
                else:
                    props[field_name] = val

            # Set assignedDistrict property for backward compatibility and initial load
            if district_field and district_field in props:
                props["assignedDistrict"] = props[district_field]
            else:
                props["assignedDistrict"] = None

            geom = row[len(all_fields)]

            if not geom:
                continue

            # ✅ projection fix
            g = geom.projectAs(wgs84)

            esri_json = json.loads(g.JSON)

            geo_geom = esri_to_geojson(esri_json)

            if not geo_geom:
                continue

            features.append({
                "type": "Feature",
                "geometry": geo_geom,
                "properties": props
            })

            count += 1

        except Exception as e:
            print("Feature error:", str(e))
            continue

    del cursor

    print("✅ Returned features:", count)

    return jsonify({
        "type": "FeatureCollection",
        "features": features
    })


# -----------------------------------
# EXPORT
# -----------------------------------
@app.route('/api/export', methods=['POST'])
def export_data():

    data = request.get_json()

    out_path = os.path.join(os.getcwd(), "districts_output.geojson")

    with open(out_path, "w") as f:
        json.dump(data, f)

    print("✅ Exported:", out_path)

    return send_file(out_path, as_attachment=True)


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
# AUTO-DISTRICT OPTIMIZATION SOLVER (SIMULATED ANNEALING - PYTHON 2/ARCPY COMPATIBLE)
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
        print "Running Auto-District Solver (ArcPy Python 2): Districts=%d, Field=%s, Compactness=%s, Restarts=%d, SeedMode=%s" % (
            num_districts, str(value_field), str(compactness_weight), num_restarts, str(seed_mode)
        )
        
        # Filter valid polygon features
        valid_features = []
        for feat in features:
            if not feat or 'geometry' not in feat or not feat['geometry']:
                continue
            g_type = feat['geometry'].get('type')
            if g_type in ['Polygon', 'MultiPolygon']:
                valid_features.append(feat)
                
        features = valid_features
        if len(features) == 0:
            yield json.dumps({"type": "error", "error": "No valid spatial polygons found in the dataset."}) + "\n"
            return
            
        num_districts = min(num_districts, len(features))

        # Dynamic district names list generation
        if not district_names:
            district_names = ["District " + str(i+1) for i in range(num_districts)]
        else:
            if len(district_names) < num_districts:
                for i in range(len(district_names), num_districts):
                    cand = "District " + str(i+1)
                    idx = i + 1
                    while cand in district_names:
                        idx += 1
                        cand = "District " + str(idx)
                    district_names.append(cand)
            elif len(district_names) > num_districts:
                district_names = district_names[:num_districts]

        # Convert GeoJSON geometries to ArcPy shape objects
        print "Converting geometries to ArcPy shapes..."
        arcpy_geoms = []
        for feat in features:
            arcpy_geom = arcpy.AsShape(feat['geometry'], True)
            arcpy_geoms.append(arcpy_geom)

        # 1. BUILD SPATIAL INDEX ADJACENCY GRAPH
        # Bounding box extents pre-filtering for fast planar neighbor checking
        print "Building spatial adjacency graph..."
        extents = []
        for g in arcpy_geoms:
            ext = g.extent
            extents.append((ext.XMin, ext.YMin, ext.XMax, ext.YMax))
            
        neighbors = {}
        total_edges = 0
        n_features = len(features)
        
        for i in range(n_features):
            neighbors[i] = []
            
        for i in range(n_features):
            geom_i = arcpy_geoms[i]
            ext_i = extents[i]
            for j in range(i + 1, n_features):
                ext_j = extents[j]
                # AABB overlap check
                if not (ext_i[2] < ext_j[0] or ext_i[0] > ext_j[2] or ext_i[3] < ext_j[1] or ext_i[1] > ext_j[3]):
                    # Bounding boxes overlap! Perform precise ArcPy touches/intersect check
                    geom_j = arcpy_geoms[j]
                    try:
                        if geom_i.touches(geom_j):
                            # Ensure non-zero shared boundary length (not point-contact)
                            overlap = geom_i.intersect(geom_j, 2) # 2 = Polyline
                            if overlap and overlap.length > 1e-6:
                                neighbors[i].append(j)
                                neighbors[j].append(i)
                                total_edges += 2
                        elif geom_i.contains(geom_j) or geom_j.contains(geom_i):
                            overlap = geom_i.intersect(geom_j, 2)
                            if overlap and overlap.length > 1e-6:
                                neighbors[i].append(j)
                                neighbors[j].append(i)
                                total_edges += 2
                    except Exception:
                        pass
                        
        total_edges = total_edges // 2
        print "Adjacency graph built: %d nodes, %d edges" % (n_features, total_edges)

        # Map custom seeds (_poly_id properties to row index)
        custom_seed_indices = {}
        if custom_seeds:
            for d_name, poly_id in custom_seeds.items():
                try:
                    target_id = int(poly_id)
                    for idx, feat in enumerate(features):
                        if feat.get('properties', {}).get('_poly_id') == target_id:
                            custom_seed_indices[d_name] = idx
                            break
                except (ValueError, TypeError):
                    pass

        # Extract values helper
        def get_val(idx):
            val = 1.0
            if value_field:
                try:
                    feat_val = features[idx].get('properties', {}).get(value_field)
                    if feat_val is not None:
                        val = float(feat_val)
                        if math.isnan(val) or math.isinf(val):
                            val = 1.0
                except (ValueError, TypeError):
                    val = 1.0
            return val

        # 2. SEED SELECTION & OPTIMIZATION LOOP (WITH RANDOM RESTARTS)
        best_energy = float('inf')
        best_assignments = None
        best_sums = None

        coords = []
        for g in arcpy_geoms:
            cent = g.centroid
            coords.append((cent.X, cent.Y))

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
                elif seed_mode == 'assigned':
                    matching_indices = []
                    for idx, feat in enumerate(features):
                        if feat.get('properties', {}).get('assignedDistrict') == d_name:
                            matching_indices.append(idx)
                    if matching_indices:
                        best_idx = max(matching_indices, key=lambda idx: get_val(idx))
                        if best_idx not in seeds:
                            seeds.append(best_idx)

            # Phase 2b: Fill any remaining seeds using furthest-first
            while len(seeds) < num_districts:
                if not seeds:
                    # If no seeds are defined (auto mode or no manual seeds yet)
                    if restart_idx == 0:
                        # Centroid baseline
                        avg_x = sum(c[0] for c in coords) / float(len(coords))
                        avg_y = sum(c[1] for c in coords) / float(len(coords))
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
            district_assignments = [None] * len(features)
            district_sums = [0.0] * num_districts
            for d_idx, s_idx in enumerate(seeds):
                district_assignments[s_idx] = d_idx
                district_sums[d_idx] = get_val(s_idx)

            unassigned_count = len(features) - num_districts
            while unassigned_count > 0:
                frontier_candidates = []
                for idx in range(len(features)):
                    if district_assignments[idx] is not None:
                        continue
                    for n in neighbors[idx]:
                        adj_d = district_assignments[n]
                        if adj_d is not None:
                            frontier_candidates.append((idx, adj_d))
                
                if not frontier_candidates:
                    # Handle disconnected spatial components
                    for idx in range(len(features)):
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

            ideal_target = sum(district_sums) / float(num_districts) if num_districts > 0 else 0.0

            # Run annealing
            temp = 0.1
            alpha = 0.9995
            steps = 15000
            
            for step in range(steps):
                temp = temp * alpha
                
                # Select random boundary node
                u = random.randrange(len(features))
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
                        prob = math.exp(-delta_E / float(temp))
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
            for u in range(len(features)):
                d_u = district_assignments[u]
                for n in neighbors[u]:
                    if district_assignments[n] != d_u:
                        cut_edges += 1
            cut_edges = cut_edges // 2
            comp_energy = cut_edges * 0.05
            
            final_energy = (1.0 - compactness_weight) * bal_energy + compactness_weight * comp_energy
            print "Restart %d/%d completed. Final Energy: %.4f (Balance: %.4f, Cuts: %d)" % (
                restart_idx + 1, num_restarts, final_energy, bal_energy, cut_edges
            )
            
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
        print "Best solution chosen with Energy: %.4f. Optimized balance sums: %s" % (best_energy, str(district_sums))

        # 5. WRITE BACK NEW ASSIGNMENTS TO FEATURE PROPERTIES
        for idx, d_idx in enumerate(district_assignments):
            if 'properties' not in features[idx]:
                features[idx]['properties'] = {}
            features[idx]['properties']['assignedDistrict'] = district_names[d_idx]

        # Convert entire optimized dataset back to JSON FeatureCollection
        geojson_data = {
            "type": "FeatureCollection",
            "features": features
        }
        yield json.dumps({
            "type": "result",
            "data": geojson_data
        }) + "\n"
        return

    except Exception as e:
        import traceback
        print "Auto-districting solver error:"
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
# RUN
# -----------------------------------
if __name__ == '__main__':
    app.run(debug=False, use_reloader=False, port=5000, threaded=True)

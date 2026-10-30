UPDATE: automatic slip lanes + exit lanes from NRN geometry
Copy these files into Junction_API (replace):  main.py, slip_measure.py (new),
requirements.txt, frontend/index.html
Export from QGIS into Junction_API\data (Format GeoJSON, CRS EPSG:4326):
  NRN network layer      -> NRN_Network.geojson     (fields: FNODE, TNODE, ONEWAY, LANES, NAME_EN)
  NRN junction box layer -> NRN_JunctionBox.geojson (field: SITE_ID)
Then:  pip install -r requirements.txt   and   python -m uvicorn main:app --reload

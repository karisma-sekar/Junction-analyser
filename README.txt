JUNCTION WORKBENCH - SETUP (version 2: stage picture + satellite)
1. Copy Junctions.geojson next to main.py. Copy the 4 input files into "data".
2. Delete the old "cache" folder if you have one from version 1.
3. Command Prompt in this folder:
     pip install -r requirements.txt
     python -m uvicorn main:app --reload
   First start reads all stage pictures (2-3 minutes). Later starts are fast.
4. Browser: http://127.0.0.1:8000/frontend/
5. Per junction: Search -> Auto-fill from stage picture -> check picture + notes
   -> Satellite: measure slip entry/exit, check exit lanes -> set "confirmed": true
   -> Save config & check -> Download TXT.

import sys
import stage_reader as sr

path = sys.argv[1]
pic = sr.read_picture(path, list("ABCDEFGHIJKLMNOP"))
print("STAGES:", pic.get("stages"))
print("NOTES :", pic.get("notes"))

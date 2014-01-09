import os, sys, re, errno

import polib

def get_module_path():
    """Return the folder containing this script (or its .exe)."""
    module_name = sys.executable if hasattr(sys, 'frozen') else __file__
    abs_path = os.path.abspath(module_name)
    return os.path.dirname(abs_path)


def print_usage():
    """Print usage message and exit."""
    print 'Usage: po_to_mo <pofile> (or drag pofile onto executable icon).'
    raw_input()
    sys.exit(1)

if len(sys.argv) < 2:
    print len(sys.argv)
    print_usage()

po_filename = sys.argv[1]
print 'Got filename', po_filename

match = re.search(r'guiminer_(.*).po', po_filename)
if match is None:
    print_usage()
else:
    language_code = match.group(1)

po = polib.pofile(po_filename)

folder = os.path.join(get_module_path(), 'locale', language_code, 'LC_MESSAGES')
try:
    os.makedirs(folder)
except OSError as exc:
    if exc.errno != errno.EEXIST:
        raise

path = os.path.join(folder, 'guiminer.mo')
try:
    po.save_as_mofile(path)
except:
    print "Couldn't save file"
    raise
else:
    print "Save OK. Press any key to continue."
    raw_input()

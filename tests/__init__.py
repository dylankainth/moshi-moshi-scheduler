import os
import tempfile

os.environ["FORESIGHT_HOME"] = tempfile.mkdtemp(prefix="foresight-test-")

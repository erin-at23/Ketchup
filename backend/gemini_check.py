"""Quick check that your Gemini key + model work. Run: python gemini_check.py"""
import time
import main

sample = ("For the matrix two one one two, we solve det of A minus lambda I equals zero. "
          "That gives lambda squared minus four lambda plus three, so the eigenvalues are three and one.")
print(f"model={main.GEMINI_MODEL} thinking={main.GEMINI_THINKING} key_set={bool(main.GEMINI_API_KEY)}")
t0 = time.time()
try:
    print("recap:", main.call_gemini(sample))
    print(f"OK in {time.time() - t0:.1f}s (aim for < 3s)")
except Exception as e:
    print(f"FAILED after {time.time() - t0:.1f}s: {type(e).__name__}: {e}")

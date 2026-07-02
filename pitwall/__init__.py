"""Pitwall — a conversational F1 race-engineer data lab.

Claude acts as a race-strategy engineer. When you ask a question, it writes
Python (pandas/matplotlib) and runs it inside an AWS Lambda MicroVM over open
Formula 1 data, then explains the result.

Each chat session gets its own microVM (``pitwall.microvm.MicroVMSandbox``),
giving real VM-level isolation for the untrusted, model-generated code — exactly
what Lambda MicroVMs are built for.
"""

__version__ = "0.1.0"

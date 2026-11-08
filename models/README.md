# Wake word models

Set `WAKEWORD_MODEL_PATH` in the root `.env` to an existing openWakeWord `.onnx`
model, using an absolute path or one relative to the project root. For a custom
SBEM wake word, obtain or train that model separately and place it here:

```dotenv
WAKEWORD_MODEL_PATH=models/sbem.onnx
WAKEWORD_THRESHOLD=0.5
CONVERSATION_IDLE_SECS=30
```

A classifier alone is insufficient: openWakeWord also needs its shared
`melspectrogram.onnx` and `embedding_model.onnx` feature models in its package
resources directory. The app does not download models during startup.

After `uv sync`, you can explicitly download the official feature models and
an existing English wake word for testing:

```bash
uv run python -c "from openwakeword.utils import download_models; download_models(['hey_jarvis'])"
uv run python -c "import openwakeword; print('\n'.join(openwakeword.get_pretrained_model_paths('onnx')))"
```

Set `WAKEWORD_MODEL_PATH` to the printed path for the downloaded `hey_jarvis`
ONNX model, then say “hey Jarvis.” That model does not recognize “SBEM.” A custom
SBEM classifier can use the downloaded feature models with its own local path.
The upstream download helper also fetches the corresponding TFLite assets and
VAD resources; those are not used by this app's ONNX wake detector.

Python 3.11 is selected for this Jetson workspace. openWakeWord 0.6.0 declares
a Linux TFLite runtime dependency even though this application selects ONNX.
Package and model installation requires internet access; ARM64 package installation
and inference on your chosen model still need validation on the robot.

See the [openWakeWord project](https://github.com/dscripka/openWakeWord/tree/v0.6.0)
for model training, language coverage, and licensing of pretrained models.
Model files in this folder are ignored by Git.

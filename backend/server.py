from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from detector import StudyGuardDetector
import os

app = FastAPI()

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize Detector
try:
    detector = StudyGuardDetector()
    print("Model loaded successfully")
except Exception as e:
    print(f"Failed to load model: {e}")
    detector = None

# Mount Static Files (Frontend)
static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir)
# app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.websocket("/ws/detect")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print("Client connected")

    if detector is None:
        await websocket.close(code=1011, reason="Model not loaded")
        return

    try:
        while True:
            # 1. Receive raw bytes from test.py/Postman
            data = await websocket.receive_bytes()

            if detector is not None:
                # 2. Call the REAL function name from your detector.py
                results = detector.detect_phone(data)

                # 3. Send back the results
                await websocket.send_json({"status": "ok", "detections": results})

    except WebSocketDisconnect:
        print("Client disconnected")
    except Exception as e:
        print(f"Error during detection: {e}")

    except WebSocketDisconnect:
        print("Client disconnected")
    except Exception as e:
        print(f"Error during detection: {e}")

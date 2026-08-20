import asyncio
import websockets


async def test_detection():
    uri = "ws://localhost:8000/ws/detect"
    # Replace 'your_image.png' with a real image file in your folder
    image_path = "test_phone.png"

    async with websockets.connect(uri) as websocket:
        print(f"Connected to {uri}")

        with open(image_path, "rb") as f:
            image_bytes = f.read()

        print(f"Sending {len(image_bytes)} bytes...")
        await websocket.send(image_bytes)

        # Wait for the JSON response from your FastAPI server
        response = await websocket.recv()
        print(f"Response from server: {response}")


if __name__ == "__main__":
    asyncio.run(test_detection())

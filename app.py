from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Optional
import subprocess
import signal
import os
from datetime import datetime
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="CPU Stresser API", version="1.0.0")

# Serve static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Store running stress processes
running_stresses = {}


class StressRequest(BaseModel):
    cpu: int = Field(..., gt=0, description="Number of CPU workers to stress")
    timeout: int = Field(..., gt=0, description="Duration in seconds to run stress")


class StressResponse(BaseModel):
    message: str
    cpu: int
    timeout: int
    process_id: Optional[int] = None


@app.get("/")
async def root():
    """Serve the HTML UI"""
    return FileResponse("static/index.html")


@app.get("/health")
async def health():
    """Health check endpoint for upstream health checks"""
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}


@app.post("/stress", response_model=StressResponse)
async def start_stress(request: StressRequest, background_tasks: BackgroundTasks):
    """
    Start CPU stress test using stress-ng
    
    - **cpu**: Number of CPU workers to stress
    - **timeout**: Duration in seconds to run the stress test
    """
    try:
        # Check if stress-ng is available
        subprocess.run(["which", "stress-ng"], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        raise HTTPException(
            status_code=500,
            detail="stress-ng is not installed. Please install it: apt-get install stress-ng"
        )
    
    # Build stress-ng command
    cmd = [
        "stress-ng",
        "--cpu", str(request.cpu),
        "--timeout", f"{request.timeout}s",
        "--metrics-brief"
    ]
    
    try:
        # Start stress-ng process
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid  # Create new process group
        )
        
        process_id = process.pid
        running_stresses[process_id] = process
        
        logger.info(f"Started stress-ng: PID {process_id}, CPU={request.cpu}, timeout={request.timeout}s")
        
        # Clean up process after completion
        background_tasks.add_task(cleanup_process, process_id, request.timeout)
        
        return StressResponse(
            message=f"CPU stress started with {request.cpu} workers for {request.timeout} seconds",
            cpu=request.cpu,
            timeout=request.timeout,
            process_id=process_id
        )
    except Exception as e:
        logger.error(f"Failed to start stress-ng: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to start stress test: {str(e)}")


@app.delete("/stress/{process_id}")
async def stop_stress(process_id: int):
    """Stop a running stress test by process ID"""
    if process_id not in running_stresses:
        raise HTTPException(status_code=404, detail="Stress process not found")
    
    process = running_stresses[process_id]
    try:
        # Kill the process group
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        process.wait(timeout=5)
        del running_stresses[process_id]
        logger.info(f"Stopped stress-ng: PID {process_id}")
        return {"message": f"Stress process {process_id} stopped successfully"}
    except ProcessLookupError:
        del running_stresses[process_id]
        return {"message": f"Process {process_id} was already terminated"}
    except Exception as e:
        logger.error(f"Failed to stop stress-ng: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to stop stress test: {str(e)}")


@app.get("/stress")
async def list_stresses():
    """List all running stress processes"""
    active_processes = []
    for pid, process in list(running_stresses.items()):
        if process.poll() is None:  # Process is still running
            active_processes.append({"process_id": pid})
        else:
            # Clean up finished processes
            del running_stresses[pid]
    
    return {
        "active_stresses": active_processes,
        "count": len(active_processes)
    }


async def cleanup_process(process_id: int, timeout: int):
    """Background task to clean up finished processes"""
    import asyncio
    await asyncio.sleep(timeout + 1)  # Wait a bit longer than timeout
    if process_id in running_stresses:
        process = running_stresses[process_id]
        if process.poll() is not None:  # Process has finished
            del running_stresses[process_id]
            logger.info(f"Cleaned up finished stress process: PID {process_id}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)

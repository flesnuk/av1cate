import asyncio
import shlex
import uuid
import re
from pathlib import Path
from typing import List, Optional, Dict
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

# --- ESTADOS Y MODELOS DE DATOS ---
class JobStatus:
    PENDING = "pendiente"
    PROCESSING = "procesando"
    COMPLETED = "completada"
    ERROR = "error"

class JobCreate(BaseModel):
    input_path: str = Field(..., description="Ruta completa del vídeo mp4/mkv")
    preset: int = Field(default=4, ge=1, le=10)
    crf: int = Field(default=35, ge=10, le=60)
    optional_params: str = Field(default="", description="Parámetros extra para SvtAv1EncApp")
    encode_opus: bool = Field(default=False, description="Convertir audio a Opus")
    opus_quality: int = Field(default=128, description="Calidad del Opus en kbps")
    tune: int = Field(default=0, ge=0, le=5, description="Tune (0 a 5)")

class Job(JobCreate):
    id: str
    status: str = JobStatus.PENDING
    error_message: Optional[str] = None
    log_file: Optional[str] = None
    current_progress: Optional[str] = None
    final_summary: Optional[str] = None

# --- VARIABLES GLOBALES (ESTADO DEL SERVIDOR) ---
jobs_db: Dict[str, Job] = {}
queue: List[str] = []

is_running = False
current_process: Optional[asyncio.subprocess.Process] = None
current_job_id: Optional[str] = None

ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')

def get_last_progress(log_file: str) -> str:
    """Lee el final del fichero temporal, limpia los colores ANSI y extrae la última línea útil."""
    try:
        with open(log_file, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            # Leemos solo los últimos 2KB para no cargar el fichero entero si es grande
            if size > 2048:
                f.seek(size - 2048)
            else:
                f.seek(0)
            content = f.read().decode('utf-8', errors='ignore')
            
            # SvtAv1EncApp usa \r para sobrescribir, así que cortamos por ahí
            lines = content.replace('\n', '\r').split('\r')
            for line in reversed(lines):
                if "Encoding:" in line:
                    # Quitamos colores y normalizamos espacios
                    clean_line = ANSI_ESCAPE.sub('', line)
                    return " ".join(clean_line.split())
    except Exception:
        pass
    return ""

# --- LÓGICA DE EJECUCIÓN (WORKER) ---
async def run_command(cmd: List[str], log_file: Optional[str] = None):
    """Ejecuta un comando en el sistema y permite su interrupción."""
    global current_process, is_running
    
    if not is_running:
        raise InterruptedError("Cancelado por el usuario (Stop).")

    print(f"Ejecutando: {' '.join(cmd)}")
    
    f_log = None
    try:
        if log_file:
            # Redirigimos el stderr al fichero si se nos pide (para sacar telemetría)
            f_log = open(log_file, "wb")
            current_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=f_log
            )
            await current_process.wait()
        else:
            current_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE
            )
            # communicate() es más seguro cuando usamos PIPE para evitar deadlocks de memoria
            _, stderr = await current_process.communicate()
        
        if current_process.returncode != 0:
            if not is_running:
                raise InterruptedError("Cancelado por el usuario (Stop).")
            else:
                err_msg = "Error desconocido"
                if not log_file and stderr:
                    err_msg = stderr.decode(errors='ignore')
                elif log_file:
                    err_msg = f"El comando falló. Revisa el log: {log_file}"
                raise RuntimeError(f"El comando falló con código {current_process.returncode}: {err_msg}")
    finally:
        if f_log:
            f_log.close()

async def process_job(job: Job):
    """Procesa todas las fases de codificación de un trabajo."""
    input_path = Path(job.input_path)
    if not input_path.is_file():
        raise FileNotFoundError(f"El archivo {input_path} no existe.")

    base_name = input_path.stem
    dir_name = input_path.parent
    
    video_ivf = dir_name / f"{base_name}_temp.ivf"
    audio_opus = dir_name / f"{base_name}_temp.opus"
    log_txt = dir_name / f"{base_name}_temp.txt"
    timecodes_txt = dir_name / f"{base_name}_pts.txt"
    final_mkv = dir_name / f"{base_name}_{job.preset}P{job.crf}Q.mkv"

    job.log_file = str(log_txt)

    # 1. Codificar Video (SvtAv1EncApp)
    cmd_svt = [
        "SvtAv1EncApp", "-i", str(input_path), "-b", str(video_ivf),
        "--preset", str(job.preset), "--crf", str(job.crf), "--tune", str(job.tune), "--progress", "2",
        "--color-primaries", "1", "--transfer-characteristics", "1", "--matrix-coefficients", "1"
    ]
    if job.optional_params:
        cmd_svt.extend(shlex.split(job.optional_params))
        
    await run_command(cmd_svt, log_file=str(log_txt))
    
    # Guardamos el resumen final del encoding
    job.final_summary = get_last_progress(str(log_txt))

    # 2. Codificar Audio (si corresponde)
    if job.encode_opus:
        cmd_audio = [
            "ffmpeg", "-y", "-i", str(input_path), "-vn", 
            "-c:a", "libopus", "-b:a", f"{job.opus_quality}k", "-vbr", "on", str(audio_opus)
        ]
        await run_command(cmd_audio)
        
    # Extraer timecodes originales (útil para MKV, silenciamos errores si es MP4 o falla)
    has_timecodes = False
    try:
        cmd_extract_pts = ["mkvextract", str(input_path), "timecodes_v2", f"0:{timecodes_txt}"]
        proc_pts = await asyncio.create_subprocess_exec(
            *cmd_extract_pts,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await proc_pts.wait()
        # Verificamos que haya terminado bien y que el archivo de texto no esté vacío
        if proc_pts.returncode == 0 and timecodes_txt.exists() and timecodes_txt.stat().st_size > 0:
            has_timecodes = True
    except Exception:
        pass
    
    # 3. Muxing (Unir audio y video)
    cmd_mux = ["mkvmerge", "-o", str(final_mkv)]
    
    if has_timecodes:
        cmd_mux.extend(["--timecodes", f"0:{timecodes_txt}"])
        
    if job.encode_opus:
        cmd_mux.extend([str(video_ivf), str(audio_opus)])
    else:
        cmd_mux.extend([str(video_ivf), "--no-video", str(input_path)])
        
    await run_command(cmd_mux)
    
    # 4. Añadir Metadata (mkvpropedit)
    try:
        # Obtener versión exacta del encoder
        proc_version = await asyncio.create_subprocess_exec(
            "SvtAv1EncApp", "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )
        stdout, _ = await proc_version.communicate()
        encoder_version = stdout.decode(errors='ignore').splitlines()[0].strip() if stdout else "SvtAv1EncApp"
    except Exception:
        encoder_version = "SvtAv1EncApp"

    # Preparar parámetros de vídeo como string
    video_params = f"--preset {job.preset} --crf {job.crf} --tune {job.tune}"
    if job.optional_params:
        video_params += f" {job.optional_params}"

    # Crear contenido XML para mkvpropedit
    xml_content = f"""<?xml version="1.0"?>
<!-- <!DOCTYPE Tags SYSTEM "matroskatags.dtd"> -->
<Tags>
  <Tag>
    <Simple>
      <Name></Name>
      <String></String>
    </Simple>
  </Tag>
  <Tag>
    <Targets />
    <Simple>
      <Name>ENCODER</Name>
      <String>{encoder_version}</String>
    </Simple>
    <Simple>
      <Name>ENCODER_SETTINGS</Name>
      <String>{video_params}</String>
    </Simple>
  </Tag>
</Tags>"""

    xml_file = dir_name / f"{base_name}_temp.xml"
    with open(xml_file, "w", encoding="utf-8") as f:
        f.write(xml_content)

    # Aplicar etiquetas al track de video (v1)
    cmd_propedit = ["mkvpropedit", str(final_mkv), "--tags", f"track:v1:{xml_file}"]
    await run_command(cmd_propedit)
    
    # Limpiar archivos temporales tras el éxito
    if video_ivf.exists(): video_ivf.unlink()
    if audio_opus.exists(): audio_opus.unlink()
    if log_txt.exists(): log_txt.unlink()
    if xml_file.exists(): xml_file.unlink()
    if timecodes_txt.exists(): timecodes_txt.unlink()

async def worker_loop():
    """Bucle infinito que procesa la cola cuando is_running == True."""
    global is_running, current_process, current_job_id
    
    while True:
        if is_running and queue:
            job_id = queue[0]
            current_job_id = job_id
            job = jobs_db[job_id]
            job.status = JobStatus.PROCESSING
            job.error_message = None
            
            try:
                await process_job(job)
                if job.status == JobStatus.PROCESSING: # Si nadie lo detuvo
                    job.status = JobStatus.COMPLETED
                    queue.pop(0) # Lo sacamos de la cola de pendientes
            except InterruptedError:
                # El proceso se detuvo a la mitad (Reset)
                job.status = JobStatus.PENDING
                print(f"Trabajo {job_id} interrumpido y reseteado a pendiente.")
            except Exception as e:
                # Ocurrió un error real
                job.status = JobStatus.ERROR
                job.error_message = str(e)
                queue.pop(0) # Lo sacamos para no bloquear la cola infinitamente
                print(f"Error en trabajo {job_id}: {e}")
            finally:
                current_process = None
                current_job_id = None
        
        await asyncio.sleep(1)

# --- CICLO DE VIDA DE FASTAPI ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Arrancar el worker al iniciar la app
    task = asyncio.create_task(worker_loop())
    yield
    # Detener todo al cerrar
    task.cancel()

app = FastAPI(title="AV1 Encoder API", lifespan=lifespan)

# --- ENDPOINTS (API) ---

@app.post("/api/jobs", response_model=Job)
def create_job(job_in: JobCreate):
    """Encola un nuevo trabajo."""
    job_id = str(uuid.uuid4())[:8]
    new_job = Job(id=job_id, **job_in.dict())
    jobs_db[job_id] = new_job
    queue.append(job_id)
    return new_job

@app.get("/api/jobs")
def get_jobs():
    """Lista todos los trabajos y el estado global."""
    for job in jobs_db.values():
        if job.status == JobStatus.PROCESSING and job.log_file:
            job.current_progress = get_last_progress(job.log_file)
            
    return {
        "is_running": is_running,
        "queue": queue,
        "jobs": list(jobs_db.values())
    }

@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    """Elimina un trabajo de la cola."""
    if job_id not in jobs_db:
        raise HTTPException(status_code=404, detail="Trabajo no encontrado")
        
    job = jobs_db[job_id]
    
    if job.status == JobStatus.PROCESSING:
        raise HTTPException(status_code=400, detail="No puedes eliminar un trabajo mientras se está procesando. Pulsa Stop primero.")
        
    if job_id in queue:
        queue.remove(job_id)
        
    del jobs_db[job_id]
    return {"message": "Trabajo eliminado correctamente"}

@app.post("/api/control/play")
def play_queue():
    """Inicia el procesamiento de la cola."""
    global is_running
    is_running = True
    return {"message": "Cola iniciada"}

@app.post("/api/control/stop")
async def stop_queue():
    """Detiene la cola, interrumpe el trabajo actual y borra los temporales."""
    global is_running, current_process, current_job_id
    is_running = False
    
    if current_process:
        try:
            current_process.terminate()
            # Le damos un pequeño respiro para que suelte los archivos
            await asyncio.sleep(0.5) 
        except Exception as e:
            print(f"Error terminando proceso: {e}")
            
    if current_job_id and current_job_id in jobs_db:
        job = jobs_db[current_job_id]
        input_path = Path(job.input_path)
        base_name = input_path.stem
        dir_name = input_path.parent
        
        # Archivos temporales a borrar
        temps = [
            dir_name / f"{base_name}_temp.ivf",
            dir_name / f"{base_name}_temp.opus",
            dir_name / f"{base_name}_temp.txt",
            dir_name / f"{base_name}_temp.xml",
            dir_name / f"{base_name}_pts.txt"
        ]
        
        for t in temps:
            if t.exists():
                try: t.unlink()
                except Exception as e: print(f"No se pudo borrar {t}: {e}")
                
        # Reiniciar a estado pendiente
        job.status = JobStatus.PENDING
        
    return {"message": "Cola detenida. Archivos temporales borrados y tarea reseteada."}

@app.get("/api/browse")
def browse_fs(path: Optional[str] = None):
    """Permite navegar por el sistema de ficheros del servidor."""
    if not path:
        path = "/" # En Debian, el directorio raíz por defecto
        
    p = Path(path)
    if not p.exists() or not p.is_dir():
        raise HTTPException(status_code=400, detail="Directorio inválido o no existe")
    
    items = []
    # Opción para subir al directorio padre
    if p.parent != p:
        items.append({"name": "..", "path": str(p.parent), "is_dir": True})
        
    try:
        for entry in p.iterdir():
            if entry.is_dir():
                # Omitimos carpetas ocultas para mantenerlo limpio
                if not entry.name.startswith('.'):
                    items.append({"name": entry.name, "path": str(entry), "is_dir": True})
            elif entry.suffix.lower() in ['.mp4', '.mkv', '.avi', '.webm', '.mov', '.ts']:
                items.append({"name": entry.name, "path": str(entry), "is_dir": False})
    except PermissionError:
        pass # Ignoramos carpetas que el servidor no tiene permisos para leer
        
    # Ordenar: primero carpetas, luego archivos, y alfabéticamente
    items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    
    return {"current_path": str(p), "items": items}


# --- INTERFAZ WEB INTEGRADA (HTML/CSS/JS) ---
@app.get("/")
def get_dashboard():
    """Devuelve una interfaz web simple para controlar la API."""
    html_content = """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <title>Home Server - AV1 Encoder</title>
        <style>
            body { font-family: system-ui, sans-serif; background-color: #1e1e2e; color: #cdd6f4; max-width: 1400px; margin: 0 auto; padding: 20px; }
            .card { background-color: #313244; padding: 20px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
            h1, h2 { color: #89b4fa; }
            button { cursor: pointer; padding: 10px 15px; border: none; border-radius: 5px; font-weight: bold; color: white; transition: 0.2s; }
            .btn-play { background-color: #a6e3a1; color: #1e1e2e; }
            .btn-stop { background-color: #f38ba8; color: #1e1e2e; }
            .btn-refresh { background-color: #f9e2af; color: #1e1e2e; }
            .btn-delete { background-color: #eba0ac; color: #1e1e2e; padding: 5px 10px; }
            button:active { transform: scale(0.95); }
            input[type="text"], input[type="number"] { width: 100%; padding: 8px; margin: 5px 0 15px; box-sizing: border-box; background: #181825; border: 1px solid #45475a; color: #cdd6f4; border-radius: 4px; }
            .form-row { display: flex; gap: 15px; }
            .form-row > div { flex: 1; }
            table { width: 100%; border-collapse: collapse; margin-top: 10px; }
            th, td { padding: 12px; text-align: left; border-bottom: 1px solid #45475a; }
            th { background-color: #181825; }
            .status-pendiente { color: #f9e2af; }
            .status-procesando { color: #89b4fa; font-weight: bold; }
            .status-completada { color: #a6e3a1; }
            .status-error { color: #f38ba8; }
            #server-status { font-size: 1.2em; font-weight: bold; margin-left: 15px; }
            
            /* Estilos del explorador de archivos */
            .input-group { display: flex; gap: 10px; align-items: center; margin-bottom: 15px; }
            .input-group input { margin-bottom: 0 !important; }
            .modal { display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%; background-color: rgba(0,0,0,0.7); }
            .modal-content { background-color: #313244; margin: 5% auto; padding: 20px; border-radius: 8px; width: 80%; max-width: 700px; height: 75vh; display: flex; flex-direction: column; box-shadow: 0 4px 15px rgba(0,0,0,0.3); }
            .close-modal { color: #cdd6f4; font-size: 28px; font-weight: bold; cursor: pointer; line-height: 1; }
            .close-modal:hover { color: #f38ba8; }
            .browser-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; gap: 15px; }
            .current-path-display { font-family: monospace; background: #1e1e2e; padding: 8px 12px; border-radius: 4px; flex-grow: 1; overflow-x: auto; white-space: nowrap; border: 1px solid #45475a; }
            .file-list { overflow-y: auto; flex-grow: 1; border: 1px solid #45475a; border-radius: 4px; background: #181825; }
            .file-item { padding: 12px 15px; border-bottom: 1px solid #313244; cursor: pointer; display: flex; align-items: center; gap: 12px; transition: background 0.2s; }
            .file-item:hover { background-color: #45475a; }
            .file-item:last-child { border-bottom: none; }
        </style>
    </head>
    <body>
        <h1>🎬 AV1 Encoder Server</h1>
        
        <div class="card" style="display: flex; align-items: center;">
            <button class="btn-play" onclick="control('play')">▶ PLAY COLA</button>
            <button class="btn-stop" onclick="control('stop')" style="margin-left: 10px;">⏹ STOP COLA</button>
            <span id="server-status">Cargando estado...</span>
        </div>

        <div class="card">
            <h2>Añadir Trabajo</h2>
            <form id="jobForm" onsubmit="addJob(event)">
                <label>Ruta completa del archivo (mp4 / mkv):</label>
                <div class="input-group">
                    <input type="text" id="input_path" required placeholder="/ruta/absoluta/al/video.mp4">
                    <button type="button" onclick="openBrowser(lastVisitedPath)" style="background-color: #89dceb; color: #1e1e2e; white-space: nowrap;">📁 Examinar...</button>
                </div>
                
                <div class="form-row">
                    <div><label>Preset (1-10):</label><input type="number" id="preset" value="4" min="1" max="10"></div>
                    <div><label>CRF (10-60):</label><input type="number" id="crf" value="35" min="10" max="60"></div>
                    <div><label>Tune (0-5):</label><input type="number" id="tune" value="0" min="0" max="5"></div>
                </div>
                
                <div class="form-row" style="align-items: center; margin-bottom: 15px;">
                    <div>
                        <input type="checkbox" id="encode_opus">
                        <label for="encode_opus">Convertir audio a Opus</label>
                    </div>
                    <div>
                        <label>Calidad Opus (kbps):</label>
                        <input type="number" id="opus_quality" value="128">
                    </div>
                </div>

                <label>Parámetros opcionales para SvtAv1EncApp (ej. --tx-bias 1):</label>
                <input type="text" id="optional_params" placeholder="--parametro valor">

                <button type="submit" style="background-color: #89b4fa; color: #1e1e2e; width: 100%;">+ Encolar Trabajo</button>
            </form>
        </div>

        <div class="card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                <h2 style="margin: 0;">Cola de Tareas</h2>
                <button class="btn-refresh" onclick="fetchJobs()">🔄 Actualizar</button>
            </div>
            <table>
                <thead><tr><th>ID</th><th>Archivo</th><th>Preset / CRF</th><th>Extras</th><th>Estado</th><th>Acciones</th></tr></thead>
                <tbody id="jobsTable"></tbody>
            </table>
        </div>

        <!-- Modal del Explorador de Archivos -->
        <div id="browserModal" class="modal">
            <div class="modal-content">
                <div class="browser-header">
                    <div class="current-path-display" id="currentPathDisplay">/</div>
                    <span class="close-modal" onclick="closeBrowser()">&times;</span>
                </div>
                <div class="file-list" id="fileList">
                    <!-- Los archivos se inyectan aquí -->
                </div>
            </div>
        </div>

        <script>
            // --- Lógica del Explorador de Archivos ---
            const browserModal = document.getElementById('browserModal');
            let lastVisitedPath = '';
            
            async function openBrowser(path = '') {
                try {
                    const targetPath = path || lastVisitedPath || '';
                    const res = await fetch(`/api/browse?path=${encodeURIComponent(targetPath)}`);
                    if (!res.ok) throw new Error('No se pudo leer el directorio. ¿Permisos?');
                    const data = await res.json();
                    
                    lastVisitedPath = data.current_path;
                    document.getElementById('currentPathDisplay').innerText = data.current_path;
                    const fileList = document.getElementById('fileList');
                    fileList.innerHTML = '';
                    
                    data.items.forEach(item => {
                        const div = document.createElement('div');
                        div.className = 'file-item';
                        div.innerHTML = `<span>${item.is_dir ? '📁' : '🎞️'}</span> <span>${item.name}</span>`;
                        div.onclick = () => {
                            if (item.is_dir) {
                                openBrowser(item.path);
                            } else {
                                document.getElementById('input_path').value = item.path;
                                closeBrowser();
                            }
                        };
                        fileList.appendChild(div);
                    });
                    
                    browserModal.style.display = 'block';
                } catch (e) {
                    alert('Error: ' + e.message);
                }
            }

            function closeBrowser() {
                browserModal.style.display = 'none';
            }

            window.onclick = function(event) {
                if (event.target == browserModal) {
                    closeBrowser();
                }
            }

            // --- Lógica de la Cola y Trabajos ---
            async function fetchJobs() {
                try {
                    const res = await fetch('/api/jobs');
                    const data = await res.json();
                    
                    const statusText = data.is_running ? '<span style="color:#a6e3a1">🟢 Procesando cola</span>' : '<span style="color:#f38ba8">🔴 Cola pausada</span>';
                    document.getElementById('server-status').innerHTML = statusText;

                    const tbody = document.getElementById('jobsTable');
                    tbody.innerHTML = '';
                    
                    data.jobs.forEach(job => {
                        const filename = job.input_path.split('/').pop();
                        const tr = document.createElement('tr');
                        tr.innerHTML = `
                            <td>${job.id}</td>
                            <td title="${job.input_path}">${filename} ${job.encode_opus ? '🎶' : ''}</td>
                            <td>${job.preset} / ${job.crf}</td>
                            <td><span style="font-family: monospace; color: #a6adc8; font-size: 0.9em;">${job.optional_params || '-'}</span></td>
                            <td>
                                <span class="status-${job.status}">${job.status.toUpperCase()}</span>
                                ${job.status === 'procesando' && job.current_progress ? '<br><small style="color: #89dceb; font-family: monospace; font-size: 0.85em; display: inline-block; margin-top: 4px;">' + job.current_progress + '</small>' : ''}
                                ${job.status === 'completada' && job.final_summary ? '<br><small style="color: #a6e3a1; font-family: monospace; font-size: 0.85em; display: inline-block; margin-top: 4px;">' + job.final_summary + '</small>' : ''}
                                ${job.error_message ? '<br><small style="color: #f38ba8; display: inline-block; margin-top: 4px;">' + job.error_message + '</small>' : ''}
                            </td>
                            <td>
                                ${job.status !== 'procesando' ? `<button class="btn-delete" onclick="deleteJob('${job.id}')">Eliminar</button>` : ''}
                            </td>
                        `;
                        tbody.appendChild(tr);
                    });
                } catch (e) {
                    console.error("Error fetching jobs", e);
                }
            }

            async function control(action) {
                await fetch(`/api/control/${action}`, { method: 'POST' });
                fetchJobs();
            }

            async function deleteJob(id) {
                if(confirm('¿Eliminar esta tarea?')) {
                    await fetch(`/api/jobs/${id}`, { method: 'DELETE' });
                    fetchJobs();
                }
            }

            async function addJob(e) {
                e.preventDefault();
                const payload = {
                    input_path: document.getElementById('input_path').value,
                    preset: parseInt(document.getElementById('preset').value),
                    crf: parseInt(document.getElementById('crf').value),
                    tune: parseInt(document.getElementById('tune').value),
                    encode_opus: document.getElementById('encode_opus').checked,
                    opus_quality: parseInt(document.getElementById('opus_quality').value),
                    optional_params: document.getElementById('optional_params').value
                };

                await fetch('/api/jobs', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                
                document.getElementById('input_path').value = '';
                fetchJobs();
            }

            // Cargar la tabla solo al iniciar la página
            fetchJobs();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

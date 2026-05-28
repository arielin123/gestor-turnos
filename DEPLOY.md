# 🚀 Guía de Deploy — GestorTurnos en Render + PostgreSQL

## Paso 1 — Preparar los archivos

Reemplazá estos archivos en tu carpeta `horarios/`:

| Archivo nuevo        | Reemplaza           | Acción                        |
|----------------------|---------------------|-------------------------------|
| `app_pg.py`          | `app.py`            | Renombrarlo a `app.py`        |
| `requirements.txt`   | `requirements.txt`  | Reemplazar                    |
| `Procfile`           | (nuevo)             | Agregar en la raíz del proyecto |
| `render.yaml`        | (nuevo)             | Agregar en la raíz del proyecto |

Estructura final de la carpeta:
```
horarios/
├── app.py               ← el app_pg.py renombrado
├── scheduler.py
├── excel_import.py
├── requirements.txt     ← actualizado
├── Procfile             ← nuevo
├── render.yaml          ← nuevo
└── templates/
    └── index.html
```

---

## Paso 2 — Subir a GitHub

1. Ir a https://github.com → crear cuenta (si no tenés)
2. Crear repositorio nuevo → llamarlo `gestor-turnos` → Privado
3. Subir todos los archivos de la carpeta `horarios/`
   - Opción simple: arrastrar archivos desde el navegador en GitHub
   - Opción terminal:
     ```bash
     cd horarios
     git init
     git add .
     git commit -m "Initial deploy"
     git remote add origin https://github.com/TU_USUARIO/gestor-turnos.git
     git push -u origin main
     ```

---

## Paso 3 — Crear cuenta en Render

1. Ir a https://render.com → Sign up con GitHub (más fácil)

---

## Paso 4 — Deploy en Render

**Opción A — Automático con render.yaml (recomendado):**
1. En Render → New → Blueprint
2. Conectar el repositorio `gestor-turnos`
3. Render lee el `render.yaml` y crea automáticamente:
   - El servidor web
   - La base de datos PostgreSQL gratuita
   - La variable `DATABASE_URL` conectada

**Opción B — Manual:**
1. New → PostgreSQL → Free plan → Crear
2. New → Web Service → conectar repositorio
3. Runtime: Python
4. Build Command: `pip install -r requirements.txt`
5. Start Command: `gunicorn app:app`
6. Environment Variables → agregar `DATABASE_URL` con el valor de la DB creada

---

## Paso 5 — Primera visita

Al entrar por primera vez a `https://gestor-turnos.onrender.com`:
- La app crea automáticamente todas las tablas en PostgreSQL
- Inserta los empleados del seed (mismos que tenías)
- Si Ricardo Moena ya existía como rotativo, lo migra a especialista automáticamente

⚠️ **El plan gratuito de Render se duerme** después de 15 min sin uso.
   La primera visita del día puede tardar ~30 segundos en despertar.
   Para 3 usuarios internos esto es aceptable.

---

## Notas importantes

- **Los datos NO se pierden** entre deploys — están en PostgreSQL persistente
- **Cada push a GitHub** hace un redeploy automático de la app
- **La DB gratuita de Render** dura 90 días en plan free, luego hay que recrearla
  (o pasar a un plan de pago ~$7/mes si quieren persistencia permanente)
- **Variables de entorno**: nunca subas contraseñas al código, Render las maneja por panel

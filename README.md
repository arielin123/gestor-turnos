# GestorTurnos 🗓️

Sistema de gestión y asignación automática de horarios rotativos.

## Requisitos
- Python 3.9+
- pip

## Instalación

```bash
# 1. Instalar dependencias
pip install -r requirements.txt

# 2. Ejecutar la aplicación
python app.py
```

Luego abrí tu navegador en: **http://localhost:5000**

## Estructura del proyecto

```
horarios/
├── app.py          # Servidor Flask (API + rutas)
├── scheduler.py    # Motor de generación de horarios
├── requirements.txt
├── horarios.db     # Base de datos SQLite (se crea automáticamente)
└── templates/
    └── index.html  # Interfaz web completa
```

## Reglas del sistema

### Turnos rotativos
| Turno | Hora  | Color  | Bloque |
|-------|-------|--------|--------|
| AM    | 14:00 | Azul   | 3 días |
| PM    | 06:00 | Dorado | 4 días |
| Noche | 22:00 | Celeste| 3 días |

### Restricciones
- ✅ 17–19 días trabajados por mes
- ✅ Mínimo 2 empleados por turno por día
- ✅ Mínimo 2 domingos libres por mes por empleado
- ✅ Continuidad entre meses (el mes siguiente arranca donde terminó el anterior)
- ✅ Vacaciones respetadas (días marcados como libre)
- ✅ Feriados: rotativos trabajan, especialistas no

### Especialistas (Lun–Vie)
- Alternan AM/PM semana a semana
- Siempre en turnos opuestos entre sí
- No trabajan feriados

## Flujo de trabajo

1. **Empleados**: Agregar/eliminar empleados desde la pestaña "Empleados"
2. **Vacaciones**: Registrar períodos de vacaciones por empleado
3. **Feriados**: Marcar feriados del mes
4. **Horario**: Navegar al mes deseado → "Generar" → revisar → "Aprobar"
5. **Edición manual**: Clic en cualquier celda del calendario para cambiar el turno
6. **Aprobación**: Al aprobar, el estado se guarda para continuidad del mes siguiente

"""
Scheduler v18 - Basado en análisis de horarios reales sept/oct/nov.

HALLAZGO CLAVE del análisis:
  Los bloques NO son fijos. Varían entre 1-4 días según necesidad.
  La variabilidad NATURAL crea el escalonamiento que evita solapamiento.

  PM:    1-4 días trabajo, 1-2 días libres después
  AM:    1-4 días trabajo, 2 días libres después  
  NIGHT: 1-3 días trabajo, 3-4 días libres después
  
  Promedio real: ~2.3 días por bloque → ciclo ~15 días
  Resultado: máximo 6-7 personas trabajando el mismo día

ALGORITMO:
  1. Asignar ciclos escalonados (offset único por empleado)
  2. Ajustar longitud de bloque para que no haya >2 en el mismo turno
  3. Respetar descansos mínimos siempre
  4. Nunca shift→shift sin descanso

REGLAS DURAS:
  - Máximo 2 en AM simultáneamente (D, AM es moldeado)
  - Máximo 3 en PM simultáneamente
  - Máximo 3 en NIGHT simultáneamente
  - Descanso mínimo: PM≥1, AM≥2, NIGHT≥3
  - Semana: máx 5 días trabajados
  - Mes: 17-20 días
  - Min 1 domingo libre
"""

import calendar
from datetime import date, timedelta

SHIFT_HOURS  = {"AM": "6:00", "PM": "14:00", "PM19": "19:00", "NIGHT": "22:00", "L": "L"}
SHIFT_COLORS = {"AM": "blue", "PM": "gold", "PM19": "red", "NIGHT": "cyan", "L": "free"}

ROTATION   = ["PM", "AM", "NIGHT"]
BLOCK_MIN  = {"PM": 2, "AM": 2, "NIGHT": 2}  # minimum block length
BLOCK_MAX  = {"PM": 4, "AM": 4, "NIGHT": 3}  # maximum block length
BLOCK_PREF = {"PM": 4, "AM": 3, "NIGHT": 3}  # preferred/target block length
REST_MIN   = {"PM": 1, "AM": 2, "NIGHT": 3}
REST_IDEAL = {"PM": 2, "AM": 2, "NIGHT": 3}

# Hard cap per shift per day: max 2 rotativos per shift to keep AM/PM balanced
SHIFT_MAX = {"AM": 2, "PM": 2, "NIGHT": 2}

MIN_FREE_SUNDAYS = 1
MIN_WORK = 18
MAX_WORK = 19          # 0 de vacaciones: 18–19 días
MAX_WORK_VAC = 20      # 1–2 coinciden en alguna semana: 18–20 días
MAX_WORK_VAC3 = 21     # 3+ coinciden en alguna semana: 19–21 días
MIN_WORK_VAC3 = 19     # mínimo sube a 19 cuando hay 3+ simultáneos
WARN_WORK = 21
WEEK_MAX = 5

# ── Turno especial ────────────────────────────────────────────────────────────
# Ariel Painel: turno rotativo especial con reglas propias.
# Bloques: 06=AM(S+D), 19=PM(3-4d), 22=NIGHT(3d)
# Reglas:
#   - 06 siempre en sábado+domingo (fin de semana), 2 FDS por mes (3 si el calendario lo requiere)
#   - PROHIBIDO: 19 inmediatamente antes de 06 (solo 3h descanso)
#   - PERMITIDO: 06 → 19 directo (≥11h descanso)
#   - Máximo 5 días consecutivos
#   - post-06 → 2L, post-19 → 2-3L, post-22 → 3L
#   - Mínimo 2 bloques 22×3 por mes
#   - Regla Último Recurso: si el mes queda en 16 días, agregar 19 el lunes post-06(S+D)
#     seguido de 1L. Aplicar preferentemente al SEGUNDO bloque 06 del mes.
#   - 17-19 días trabajados (16 solo en casos imposibles geométricamente)
SPECIAL_EMPLOYEE_NAME = "Ariel Painel"
SPECIAL_EMPLOYEE_TYPE = "especial"
SPECIAL_WORK_WEEKDAYS = {0, 1, 2, 3}  # Mon=0, Tue=1, Wed=2, Thu=3 (legacy, kept for compatibility)

# Calendario rotativo Jul-Dic 2026 para Ariel Painel
# Formato: {(year, month): [(turno, ndias), ...]}
# Turnos: "06"=AM fin de semana, "19"=PM tarde, "22"=NIGHT, "L"=libre
# "19*" = día excepcional Último Recurso (cuenta como "19" en el sistema)
_ARIEL_SEQUENCES = {
    (2026,  7): [("L",3),("06",2),("L",2),("19",4),("L",2),("22",3),("L",3),("19",3),("L",2),("06",2),("L",2),("22",3)],
    (2026,  8): [("L",3),("22",3),("L",3),("19",3),("L",2),("06",2),("19",1),("L",1),("22",3),("L",3),("19",3),("L",1),("06",2),("L",1)],
    (2026,  9): [("22",3),("L",3),("19",3),("L",2),("06",2),("L",2),("22",3),("L",3),("19",3),("L",1),("06",2),("19",1),("L",2)],
    (2026, 10): [("22",3),("L",3),("19",3),("L",2),("19",3),("L",2),("06",2),("19",3),("L",2),("06",2),("L",2),("22",3),("L",1)],
    (2026, 11): [("22",3),("L",3),("06",2),("19",3),("L",2),("22",3),("L",4),("06",2),("L",2),("19",4),("L",2)],
    (2026, 12): [("22",3),("L",3),("19",3),("L",2),("06",2),("L",2),("22",3),("L",3),("19",3),("L",1),("06",2),("L",2),("22",2)],
}

# Mapeo de turnos Ariel → turnos internos del sistema
_ARIEL_SHIFT_MAP = {"06": "AM", "19": "PM19", "22": "NIGHT", "L": "L"}

def _build_ariel_schedule(year: int, month: int) -> dict:
    """
    Genera el diccionario {str(day): turno} para Ariel Painel
    según el calendario rotativo definido en _ARIEL_SEQUENCES.
    Si el mes no está en el calendario, usa NIGHT Lun-Jue como fallback.
    Turnos devueltos: AM / PM / NIGHT / L (compatible con el resto del sistema).
    """
    import calendar as cal_mod
    days_in_month = cal_mod.monthrange(year, month)[1]

    seq = _ARIEL_SEQUENCES.get((year, month))
    if seq is None:
        # Fallback: NIGHT fijo Lun-Jue (comportamiento anterior)
        result = {}
        for d in range(1, days_in_month + 1):
            wd = date(year, month, d).weekday()
            result[str(d)] = "NIGHT" if wd in {0, 1, 2, 3} else "L"
        return result

    result = {}
    d = 1
    for turno_raw, ndias in seq:
        turno = _ARIEL_SHIFT_MAP.get(turno_raw, "L")
        for _ in range(ndias):
            if d > days_in_month:
                break
            result[str(d)] = turno
            d += 1
    # Rellenar el resto con L si la secuencia es más corta que el mes
    while d <= days_in_month:
        result[str(d)] = "L"
        d += 1
    return result

# Offsets: 11 employees distributed across 17-day cycle
# Ciclo: PM(0-3) rest_PM(4-5) AM(6-8) rest_AM(9-10) NIGHT(11-13) rest_NIGHT(14-16)
# Distribución objetivo: ~3-4 PM, ~4 AM, ~3 NIGHT simultáneos como máximo
# Se evitan offsets consecutivos en la misma fase para escalonar mejor la cobertura.
# Más offsets en fase AM (6,7,8) para compensar la tendencia del generador a saturar PM.
FRESH_OFFSETS = [0, 6, 11, 2, 7, 12, 4, 8, 13, 1, 3]


def date_range(year, month):
    n = calendar.monthrange(year, month)[1]
    return [date(year, month, d) for d in range(1, n+1)]

def is_sunday(d):  return d.weekday() == 6
def is_weekday(d): return d.weekday() < 5

def vacation_days_for(eid, vacations, year, month):
    vac = set()
    for v in vacations:
        if v["employee_id"] != eid: continue
        s = date.fromisoformat(v["start_date"])
        e = date.fromisoformat(v["end_date"])
        cur = s
        while cur <= e:
            if cur.year == year and cur.month == month:
                vac.add(cur.day)
            cur += timedelta(days=1)
    return vac

def get_week_map(days):
    d2w, w2d = {}, {}
    for d in days:
        wk = d.isocalendar()[:2]
        d2w[d.day] = wk
        w2d.setdefault(wk, []).append(d.day)
    return d2w, w2d


# ─── Entry point ──────────────────────────────────────────────────────────────
def generate_schedule(year, month, employees, holidays, vacations, prev_state_map):
    days        = date_range(year, month)
    holiday_set = set(holidays)
    rotating    = [e for e in employees if e["type"] == "rotativo"]
    specialists = [e for e in employees if e["type"] == "especialista"]
    especiales  = [e for e in employees if e["type"] == "especial"]

    # Determine effective min/max work days based on peak simultaneous vacations in any day
    # Count for each day how many rotativos are on vacation simultaneously
    vac_sets = {emp["id"]: vacation_days_for(emp["id"], vacations, year, month)
                for emp in rotating}
    peak_simultaneous = max(
        (sum(1 for eid, vdays in vac_sets.items() if d.day in vdays) for d in days),
        default=0
    )

    # Si Ariel Painel (especial) tiene ≥11 días de vacaciones en el mes,
    # cuenta como 1 ausente adicional para escalar los días de trabajo de los rotativos
    ariel_vac_days_count = 0
    for emp in especiales:
        ariel_vac_days_count = len(vacation_days_for(emp["id"], vacations, year, month))
    ariel_counts_as_absent = ariel_vac_days_count >= 11

    effective_peak = peak_simultaneous + (1 if ariel_counts_as_absent else 0)

    if effective_peak >= 3:
        # 3+ ausentes coinciden en al menos 1 día → 19–21 días
        effective_min_work = MIN_WORK_VAC3
        effective_max_work = MAX_WORK_VAC3
    elif effective_peak >= 1:
        # 1–2 ausentes coinciden → 18–20 días
        effective_min_work = MIN_WORK
        effective_max_work = MAX_WORK_VAC
    else:
        # Sin vacaciones → 18–19 días
        effective_min_work = MIN_WORK
        effective_max_work = MAX_WORK

    assignments = {}
    for eid, dm in _specialists(specialists, days, holiday_set).items():
        assignments[str(eid)] = dm

    # Special employees: usar calendario rotativo de Ariel Painel
    for emp in especiales:
        dm = _build_ariel_schedule(year, month)
        assignments[str(emp["id"])] = dm

    # Build NIGHT coverage map from especiales (días donde Ariel trabaja NIGHT)
    especial_night_days = set()
    for emp in especiales:
        dm = assignments.get(str(emp["id"]), {})
        for d in days:
            if dm.get(str(d.day), "L") == "NIGHT":
                especial_night_days.add(d.day)

    assignments.update(_rotating(
        rotating, days, vacations, prev_state_map, year, month,
        effective_max_work, especial_night_days, effective_min_work
    ))

    states = {}
    for emp in rotating + especiales:
        eid = emp["id"]
        dm  = assignments.get(str(eid), {})
        last_shift = last_work = None
        trailing = 0
        for d in reversed(days):
            v = dm.get(str(d.day), "L")
            if v != "L":
                last_shift = v; last_work = d.isoformat(); break
            trailing += 1
        block_done = 0
        if last_shift:
            found_block = False
            for d in reversed(days):
                v = dm.get(str(d.day), "L")
                if v == last_shift:
                    found_block = True
                    block_done += 1
                elif found_block:
                    break
        block_pref = {"PM": 4, "AM": 3, "NIGHT": 3}
        block_remaining = max(0, block_pref.get(last_shift, 0) - block_done) if last_shift else 0

        states[str(eid)] = {
            "last_shift": last_shift,
            "last_work_date": last_work,
            "rest_served": trailing,
            "block_remaining": block_remaining,
        }

    summary  = {}
    warnings = []
    overwork = set()

    for emp in rotating + specialists + especiales:
        eid = emp["id"]
        dm  = assignments.get(str(eid), {})
        worked       = sum(1 for v in dm.values() if v != "L")
        free_sundays = sum(1 for d in days if is_sunday(d) and dm.get(str(d.day),"L")=="L")
        summary[str(eid)] = {"worked": worked, "free_sundays": free_sundays}
        if emp["type"] == "rotativo" and worked > effective_max_work:
            overwork.add(str(eid))

    # Coverage warnings — Ariel cuenta como operador nocturno
    for d in days:
        ariel_works_night = d.day in especial_night_days
        for shift in ["AM", "PM", "NIGHT"]:
            cnt = sum(1 for emp in rotating
                      if assignments.get(str(emp["id"]),{}).get(str(d.day),"L") == shift)
            if shift == "NIGHT":
                total_night = cnt + (1 if ariel_works_night else 0)
                if total_night == 0:
                    warnings.append({"day":d.day,"shift":shift,"count":0,"level":4,
                        "msg":f"Día {d.day} — NIGHT: SIN COBERTURA"})
                elif total_night == 1:
                    # Solo Ariel o solo 1 rotativo — cobertura baja
                    warnings.append({"day":d.day,"shift":shift,"count":total_night,"level":3,
                        "msg":f"Día {d.day} — NIGHT: 1 operador ({'Ariel' if ariel_works_night and cnt==0 else 'rotativo'}) (baja)"})
                # total_night >= 2 → cobertura OK, sin alerta
            else:
                if cnt == 0:
                    warnings.append({"day":d.day,"shift":shift,"count":0,"level":4,
                        "msg":f"Día {d.day} — {shift}: SIN COBERTURA"})
                elif cnt == 1:
                    lv = {"AM":1,"PM":2}[shift]
                    lb = {"AM":"baja","PM":"media"}[shift]
                    warnings.append({"day":d.day,"shift":shift,"count":cnt,"level":lv,
                        "msg":f"Día {d.day} — {shift}: 1 persona ({lb})"})

    # Build vac_days_map: {str(eid): [day, ...]} para mostrar visualmente en el frontend
    vac_days_map = {str(emp["id"]): sorted(vacation_days_for(emp["id"], vacations, year, month))
                    for emp in rotating}

    return {
        "year":year,"month":month,
        "days":[d.day for d in days],
        "weekdays":[d.strftime("%a") for d in days],
        "assignments":assignments,"employees":employees,
        "summary":summary,"states":states,"warnings":warnings,
        "overwork":list(overwork),
        "shift_hours":SHIFT_HOURS,"shift_colors":SHIFT_COLORS,
        "especial_night_days": list(especial_night_days),
        "vac_days_map": vac_days_map,
    }, states


# ─── Specialists ──────────────────────────────────────────────────────────────
def _specialists(specialists, days, holiday_set):
    if not specialists: return {}
    asgn  = {s["id"]:{} for s in specialists}
    weeks = []
    for d in days:
        w = d.isocalendar()[1]
        if w not in weeks: weeks.append(w)
    wi    = {w:i for i,w in enumerate(weeks)}

    # Separate fixed-shift specialists from alternating pair
    fixed     = [s for s in specialists if s.get("specialist_start") not in (None,"AM","PM","") 
                 or s.get("fixed_shift")]
    # Actually: fixed specialists are those with specialist_start = "FIXED_PM" or similar.
    # Simpler: treat any specialist with specialist_start="FIXED_PM" as fixed Lun-Vie PM.
    # For now: alternating pair = first two with AM/PM starts; rest = fixed PM Lun-Vie
    alternating = [s for s in specialists if s.get("specialist_start") in ("AM","PM",None,"")]
    fixed_pm    = [s for s in specialists if s.get("specialist_start") == "FIXED_PM"]

    # Handle alternating pair (Sebastian, Jose linares)
    if alternating:
        sa    = alternating[0]
        sb    = alternating[1] if len(alternating) > 1 else None
        start = sa.get("specialist_start") or "AM"
        for d in days:
            if not is_weekday(d) or d.isoformat() in holiday_set:
                asgn[sa["id"]][str(d.day)] = "L"
                if sb: asgn[sb["id"]][str(d.day)] = "L"
                continue
            w   = wi[d.isocalendar()[1]]
            sha = "AM" if (w%2==0)==(start=="AM") else "PM"
            asgn[sa["id"]][str(d.day)] = sha
            if sb: asgn[sb["id"]][str(d.day)] = "PM" if sha=="AM" else "AM"

    # Handle fixed Lun-Vie PM specialists (Ricardo Moena)
    for s in fixed_pm:
        for d in days:
            if not is_weekday(d) or d.isoformat() in holiday_set:
                asgn[s["id"]][str(d.day)] = "L"
            else:
                asgn[s["id"]][str(d.day)] = "PM"

    return asgn


# ─── Rotating ─────────────────────────────────────────────────────────────────
def _rotating(rotating, days, vacations, prev_state_map, year, month, effective_max_work=MAX_WORK, especial_night_days=None, effective_min_work=MIN_WORK):
    if especial_night_days is None:
        especial_night_days = set()
    vac      = {emp["id"]: vacation_days_for(emp["id"], vacations, year, month)
                for emp in rotating}
    ds       = [str(d.day) for d in days]
    d2w, w2d = get_week_map(days)
    num_days = len(days)

    # Adjust NIGHT shift cap: on Ariel's days, rotativos only need to fill up to 2 total
    # (Ariel = 1, so max 2 additional rotativos → but SHIFT_MAX NIGHT stays 3 overall,
    # we just use 2 as the rotativo-only cap on those days)
    night_rotativo_cap = {}  # day_num → max rotativos (excluding Ariel) in NIGHT
    for d in days:
        night_rotativo_cap[d.day] = 2   # always max 2 rotativos in NIGHT

    # Step 1: determine each employee's starting state
    start_states = {}
    for idx, emp in enumerate(rotating):
        eid  = str(emp["id"])
        prev = prev_state_map.get(emp["id"], {})
        if not prev.get("last_shift"):
            offset = FRESH_OFFSETS[idx % len(FRESH_OFFSETS)]
            prev = _offset_to_prev(offset)
        start_states[eid] = prev

    # Step 2: build schedules day by day, tracking coverage
    # Initialize coverage counter
    coverage = {str(d.day): {"AM":0,"PM":0,"NIGHT":0} for d in days}
    asgn = {str(emp["id"]): {str(d.day):"L" for d in days} for emp in rotating}

    for idx, emp in enumerate(rotating):
        eid  = str(emp["id"])
        prev = start_states[eid]
        dm   = _build_schedule(
            days, ds, vac[emp["id"]], prev, coverage, d2w, w2d, effective_max_work, effective_min_work
        )
        asgn[eid] = dm
        # Update coverage
        for dk, v in dm.items():
            if v != "L" and v in coverage.get(dk, {}):
                coverage[dk][v] += 1

    # Hard cap: enforce SHIFT_MAX per shift per day
    # For NIGHT: cap is always 2 rotativos (Ariel adds +1 on Mon-Thu but that's outside asgn)
    # Only remove from block ENDS (never mid-block) to avoid creating transitions
    for d in days:
        for shift in ["AM", "PM", "NIGHT"]:
            if shift == "NIGHT":
                smax = 2  # always max 2 rotativos in NIGHT regardless of Ariel
            else:
                smax = SHIFT_MAX[shift]
            workers = [eid for eid in asgn if asgn[eid].get(str(d.day),"L") == shift]
            if len(workers) <= smax: continue

            didx = ds.index(str(d.day))
            # Find which workers are at a block end on this day (safe to remove)
            at_end   = []
            mid_block = []
            for eid in workers:
                prev_v = asgn[eid].get(ds[didx-1],"L") if didx>0 else "L"
                next_v = asgn[eid].get(ds[didx+1],"L") if didx+1<len(ds) else "L"
                is_end = (next_v == "L" or next_v != shift)   # end of block
                is_start = (prev_v == "L" or prev_v != shift) # start of block
                worked = sum(1 for v in asgn[eid].values() if v!="L")
                if is_end or is_start:
                    at_end.append((worked, eid))
                else:
                    mid_block.append((worked, eid))

            # Sort by most days worked first (remove those who work most)
            at_end.sort(reverse=True)
            to_remove = len(workers) - smax
            removed = 0
            for _, eid in at_end:
                if removed >= to_remove: break
                asgn[eid][str(d.day)] = "L"
                removed += 1
            # If still over cap, remove from mid-block (unavoidable)
            if removed < to_remove:
                mid_block.sort(reverse=True)
                for _, eid in mid_block:
                    if removed >= to_remove: break
                    asgn[eid][str(d.day)] = "L"
                    removed += 1

    # Fill underworked employees (< MIN_WORK days):
    asgn = _fill_underworked(asgn, rotating, days, ds, vac, d2w, w2d, effective_min_work)

    # Rebalance AM/PM coverage: reduce solo-AM and solo-PM days
    # For each day where AM=1 and PM=2, try to move a PM worker to AM (if safe)
    # For each day where AM=0 and PM>=2, try to move a PM worker to AM (if safe)
    asgn = _rebalance_am_pm(asgn, rotating, days, ds, d2w, w2d, vac)

    # Final block max enforcement (fill may have created over-length blocks)
    for emp in rotating:
        eid = str(emp["id"])
        asgn[eid] = _enforce_block_max(asgn[eid], ds)

    return asgn


def _rebalance_am_pm(asgn, rotating, days, ds, d2w, w2d, vac):
    """
    Post-processing pass: reduce días con AM solo (1 o 0) cuando PM tiene exceso (≥3).

    Prioridad de cobertura del negocio: NIGHT > PM > AM.
    Este paso NO toca NIGHT. Solo reequilibra PM→AM cuando PM está saturado
    y AM está bajo cobertura.

    Dos pasadas:
    - Pasada 1 (conservadora): solo mueve trabajadores en borde de bloque PM.
    - Pasada 2 (agresiva, solo para AM=0): mueve incluso desde mid-block,
      partiendo el bloque PM en ese día. Se acepta porque sin cobertura AM
      el riesgo operativo es mayor que tener un bloque PM partido.

    Condiciones para mover PM→AM:
      1. Vecinos del trabajador deben ser L o AM (sin transición turno→turno).
         En pasada 2 (AM=0) se permite vecino PM (se parte el bloque).
      2. No estar de vacaciones ese día.
      3. No superar WEEK_MAX (PM→AM no cambia total trabajado).
      4. Dejar PM con al menos 2 trabajadores.
    """
    def _try_move(d, dk, didx, target_moves, allow_mid_block):
        pm_workers = [str(e["id"]) for e in rotating
                      if asgn[str(e["id"])].get(dk,"L") == "PM"]
        moved = 0
        for eid in pm_workers:
            if moved >= target_moves:
                break
            dm = asgn[eid]
            vset = vac.get(int(eid), set())
            if d.day in vset:
                continue

            prev_v = dm.get(ds[didx-1], "L") if didx > 0 else "L"
            next_v = dm.get(ds[didx+1], "L") if didx+1 < len(ds) else "L"

            # Vecinos deben ser L o AM (no causar turno→turno)
            if not allow_mid_block:
                if prev_v not in ("L", "AM") or next_v not in ("L", "AM"):
                    continue
            else:
                # Pasada agresiva: permitir vecino PM pero no NIGHT
                if prev_v == "NIGHT" or next_v == "NIGHT":
                    continue

            # En pasada conservadora: solo borde de bloque PM
            if not allow_mid_block:
                is_border = (prev_v != "PM") or (next_v != "PM")
                if not is_border:
                    continue

            wk = d2w[d.day]
            wk_worked = sum(1 for dn in w2d[wk] if dm.get(str(dn),"L") != "L")
            if wk_worked > WEEK_MAX:
                continue

            asgn[eid][dk] = "AM"
            moved += 1
        return moved

    # Ordenar días por urgencia: AM=0 primero, luego AM=1 con PM>=3
    def day_urgency(d):
        dk = str(d.day)
        am = sum(1 for e in rotating if asgn[str(e["id"])].get(dk,"L") == "AM")
        pm = sum(1 for e in rotating if asgn[str(e["id"])].get(dk,"L") == "PM")
        if am == 0 and pm >= 2: return 0
        if am == 1 and pm >= 3: return 1
        return 99

    # --- Pasada 1: conservadora (borde de bloque) ---
    sorted_days = sorted(days, key=day_urgency)
    for d in sorted_days:
        dk = str(d.day)
        didx = ds.index(dk)
        am_cnt = sum(1 for e in rotating if asgn[str(e["id"])].get(dk,"L") == "AM")
        pm_cnt = sum(1 for e in rotating if asgn[str(e["id"])].get(dk,"L") == "PM")
        need_move = (pm_cnt >= 3 and am_cnt <= 1) or (pm_cnt >= 2 and am_cnt == 0)
        if not need_move:
            continue
        target_moves = min(2 - am_cnt, pm_cnt - 2)
        if target_moves <= 0:
            continue
        _try_move(d, dk, didx, target_moves, allow_mid_block=False)

    # --- Pasada 2: agresiva para días que siguen con AM=0 o AM=1+PM>=3 ---
    for d in days:
        dk = str(d.day)
        didx = ds.index(dk)
        am_cnt = sum(1 for e in rotating if asgn[str(e["id"])].get(dk,"L") == "AM")
        pm_cnt = sum(1 for e in rotating if asgn[str(e["id"])].get(dk,"L") == "PM")
        # Actuar si AM=0 con PM>=2, o AM=1 con PM>=3
        if not ((am_cnt == 0 and pm_cnt >= 2) or (am_cnt == 1 and pm_cnt >= 3)):
            continue
        target_moves = min(2 - am_cnt, pm_cnt - 2)
        if target_moves <= 0:
            continue
        _try_move(d, dk, didx, target_moves, allow_mid_block=True)

    return asgn


def _offset_to_prev(offset):
    """Convert a cycle offset to a prev_state dict."""
    # Cycle: PM(0-3) rest_PM(4-5) AM(6-8) rest_AM(9-10) NIGHT(11-13) rest_NIGHT(14-16)
    cycle_map = []
    for sh in ROTATION:
        for _ in range(BLOCK_PREF[sh]):
            cycle_map.append(("work", sh))
        for j in range(REST_IDEAL[sh]):
            cycle_map.append(("rest", sh))

    if offset >= len(cycle_map):
        offset = offset % len(cycle_map)

    phase, shift = cycle_map[offset]
    prev_shift = ROTATION[(ROTATION.index(shift) - 1) % len(ROTATION)]

    if phase == "work":
        # Start of or mid work block: previous shift finished rest
        return {"last_shift": prev_shift, "rest_served": 999}
    else:
        # In rest block: figure out how many rest days served
        # Find how many rest days before this offset
        rest_start = 0
        for i, (p, s) in enumerate(cycle_map):
            if i == offset: break
            if p == "work" and s == shift:
                rest_start = i + 1

        rest_served = offset - rest_start
        if rest_served < 0: rest_served = 0
        return {"last_shift": shift, "rest_served": rest_served}


def _build_schedule(days, ds, vac_days, prev, coverage, d2w, w2d, effective_max_work=MAX_WORK, effective_min_work=MIN_WORK):
    """
    Build one employee's schedule.
    Adjusts block lengths to avoid exceeding SHIFT_MAX per shift per day.
    """
    num = len(days)
    dm  = {str(d.day): "L" for d in days}

    ls = prev.get("last_shift")
    rs = prev.get("rest_served", 0)
    br = prev.get("block_remaining", 0)  # work days still needed to finish last block

    if ls and ls in ROTATION:
        rot_idx = (ROTATION.index(ls) + 1) % len(ROTATION)
    else:
        rot_idx = 0
        br      = 0
        rs      = 0

    i = 0

    if br > 0 and ls and ls in ROTATION:
        # Case 1: block was INCOMPLETE at end of previous month.
        # Complete remaining work days (capped at block max to be safe).
        safe_br = min(br, BLOCK_PREF[ls])  # never carry over more than a full block
        for _ in range(safe_br):
            if i >= num: break
            d = days[i]
            dm[str(d.day)] = "L" if d.day in vac_days else ls
            i += 1
        # Serve full ideal rest after completing the block
        owed = REST_IDEAL[ls]
        while owed > 0 and i < num:
            d = days[i]; dm[str(d.day)] = "L"
            if d.day not in vac_days: owed -= 1
            i += 1

    elif ls and ls in ROTATION:
        # Case 2: block was complete. Check if rest was fully served.
        rest_still_owed = max(0, REST_IDEAL[ls] - rs)
        if rest_still_owed > 0:
            # Still serving rest from previous month
            owed = rest_still_owed
            while owed > 0 and i < num:
                d = days[i]; dm[str(d.day)] = "L"
                if d.day not in vac_days: owed -= 1
                i += 1
        # else: rest fully served, start next shift immediately

    while i < num:
        d = days[i]
        if d.day in vac_days:
            dm[str(d.day)] = "L"; i += 1; continue

        cur    = ROTATION[rot_idx % len(ROTATION)]
        bmin   = BLOCK_MIN[cur]
        bmax   = BLOCK_MAX[cur]
        bpref  = BLOCK_PREF[cur]
        rmin   = REST_MIN[cur]
        rideal = REST_IDEAL[cur]
        smax   = 2 if cur == "NIGHT" else SHIFT_MAX[cur]  # NIGHT: max 2 rotativos always

        total = sum(1 for v in dm.values() if v != "L")

        # Determine block length based on:
        # 1. How many days needed to reach effective_min_work
        # 2. Coverage pressure (prefer shorter block if shift is crowded)
        days_left = num - i
        needed    = max(0, effective_min_work - total)

        # Start with preferred block length
        blen = bpref

        # Shorten if shift is already at max coverage on first day of block
        first_day_cov = coverage.get(str(d.day), {}).get(cur, 0)
        if first_day_cov >= smax:
            # Shift is crowded — use minimum block and skip day if possible
            # Try to find next available day
            found_start = False
            for j in range(i, min(i+3, num)):
                dj = days[j]
                if dj.day in vac_days: continue
                cov_j = coverage.get(str(dj.day), {}).get(cur, 0)
                if cov_j < smax:
                    # Fill skipped days as L
                    while i < j:
                        dm[str(days[i].day)] = "L"; i += 1
                    found_start = True
                    break
            if not found_start:
                # Can't start this shift — skip entire block
                # Add extra rest and advance rotation
                dm[str(d.day)] = "L"; i += 1; rot_idx += 1; continue

        # Recalculate block length
        d_now = days[i]

        # Adjust block based on remaining need and coverage
        blen = bpref
        if total + bpref > effective_max_work:
            blen = min(bmax, effective_max_work - total)
        if needed > 0 and blen < bmin:
            blen = bmin

        # Work block — never exceed BLOCK_MAX consecutive days
        worked_in_block = 0
        hard_bmax = {"PM": 5, "AM": 4, "NIGHT": 3}
        for _ in range(blen):
            if i >= num: break
            d2   = days[i]
            wk   = d2w[d2.day]
            week_worked = sum(1 for dn in w2d[wk]
                              if dn < d2.day and dm.get(str(dn),"L") != "L")

            if d2.day in vac_days:
                dm[str(d2.day)] = "L"
            elif week_worked >= WEEK_MAX:
                dm[str(d2.day)] = "L"
            elif worked_in_block >= hard_bmax.get(cur, blen):
                # Hit hard block max — stop and rest
                break
            else:
                dm[str(d2.day)] = cur
                worked_in_block += 1
            i += 1

        # If block produced 0 work days (all vacations/week cap), try next rotation
        if worked_in_block == 0:
            rot_idx += 1
            continue

        # Rest block
        total = sum(1 for v in dm.values() if v != "L")
        days_left = num - i

        if total >= effective_max_work:
            rlen = days_left
        elif days_left <= rmin:
            rlen = days_left
        else:
            # Prefer REST_IDEAL always. Only reduce to REST_MIN if taking
            # ideal rest makes it impossible to reach effective_min_work.
            est_ideal = _est(days_left - rideal, rot_idx + 1)
            est_min   = _est(days_left - rmin,   rot_idx + 1)
            if total >= effective_min_work:
                rlen = rideal          # already at target → full rest
            elif total + est_ideal >= effective_min_work:
                rlen = rideal          # can still reach target → full rest
            elif total + est_min >= effective_min_work:
                rlen = rmin            # need shorter rest to reach target
            else:
                rlen = rmin            # last resort

        for _ in range(rlen):
            if i >= num: break
            dm[str(days[i].day)] = "L"; i += 1

        rot_idx += 1

    # Post-processing
    dm = _enforce_block_max(dm, ds)   # enforce PM≤5, AM≤4, NIGHT≤3
    dm = _trim_excess(dm, ds, vac_days, effective_max_work)
    dm = _fix_sunday(dm, ds, days, vac_days, effective_min_work)

    return dm


def _est(left, idx):
    t, i, x = 0, 0, idx
    while i < left:
        sh = ROTATION[x % len(ROTATION)]
        c  = BLOCK_PREF[sh] + REST_MIN[sh]
        if i + c > left:
            t += min(BLOCK_PREF[sh], left - i); break
        t += BLOCK_PREF[sh]; i += c; x += 1
    return t


def _trim_excess(dm, ds, vac_days, effective_max_work=MAX_WORK):
    worked = sum(1 for k in ds if dm.get(k,"L") != "L")
    while worked > effective_max_work:
        trimmed = False
        for dk in sorted(ds, key=int, reverse=True):
            if dm.get(dk,"L") == "L" or int(dk) in vac_days: continue
            idx    = ds.index(dk)
            next_k = ds[idx+1] if idx+1 < len(ds) else None
            if next_k is None or dm.get(next_k,"L") == "L":
                dm[dk] = "L"; worked -= 1; trimmed = True; break
        if not trimmed: break
    return dm


def _fix_sunday(dm, ds, days, vac_days, effective_min_work=MIN_WORK):
    """
    Ensure at least 1 free Sunday.
    When removing a Sunday shift, try to compensate with an adjacent non-Sunday day
    so total worked days stay >= effective_min_work.
    """
    sundays  = [d for d in days if is_sunday(d)]
    free_su  = [d for d in sundays if dm.get(str(d.day),"L") == "L"]
    if len(free_su) >= MIN_FREE_SUNDAYS: return dm

    worked_su = [d for d in sundays if dm.get(str(d.day),"L") != "L"
                 and d.day not in vac_days]
    needed = MIN_FREE_SUNDAYS - len(free_su)

    for d in worked_su:
        if needed <= 0: break
        old_shift = dm[str(d.day)]
        dm[str(d.day)] = "L"
        worked_now = sum(1 for k in ds if dm.get(k,"L") != "L")

        if worked_now < effective_min_work:
            # Try to compensate: find a free adjacent non-Sunday day
            didx = ds.index(str(d.day))
            compensated = False
            for dlt in [-1, 1, -2, 2, -3, 3]:
                ni = didx + dlt
                if not (0 <= ni < len(ds)): continue
                nk  = ds[ni]
                ndn = int(nk)
                if ndn in vac_days: continue
                if dm.get(nk,"L") != "L": continue
                ndate = date(d.year, d.month, ndn)
                if is_sunday(ndate): continue
                # No bad transition
                prev_v = dm.get(ds[ni-1],"L") if ni > 0 else "L"
                next_v = dm.get(ds[ni+1],"L") if ni+1 < len(ds) else "L"
                if prev_v != "L" and prev_v != old_shift: continue
                if next_v != "L" and next_v != old_shift: continue
                dm[nk] = old_shift
                compensated = True
                break

            if not compensated:
                dm[str(d.day)] = old_shift  # revert — can't compensate
                continue

        needed -= 1

    return dm


def _fill_underworked(asgn, rotating, days, ds, vac, d2w, w2d, effective_min_work=MIN_WORK):
    """
    Fix employees with fewer than effective_min_work days.
    Two strategies (in order):
    1. Shorten post-PM rest by 1 day (if next day after rest is PM, pull it forward)
    2. Add as 3rd person to PM or AM shifts adjacent to existing blocks
    Never creates shift→shift transitions. Never exceeds WEEK_MAX.
    """
    for emp in rotating:
        eid  = str(emp["id"])
        dm   = asgn[eid]
        vset = vac.get(emp["id"], set())
        worked = sum(1 for v in dm.values() if v != "L")
        if worked >= effective_min_work: continue

        # Strategy 1: shorten post-PM rest (1 day instead of 2)
        # Find days where: prev2=PM, prev1=L, curr=L, next=PM (or next block start)
        for j in range(1, len(ds)-1):
            if worked >= effective_min_work: break
            dk   = ds[j]
            prev = dm.get(ds[j-1], "L")
            curr = dm.get(dk, "L")
            if prev != "PM" or curr != "L": continue
            if int(dk) in vset: continue
            # Check: next day would be start of next block (not PM rest continuation)
            # Only shorten if this is the 2nd rest day (already served 1)
            if j >= 2 and dm.get(ds[j-2], "L") == "L": continue  # already 2+ rest
            # Check no bad transition: next day must be L or same shift
            next_v = dm.get(ds[j+1], "L") if j+1 < len(ds) else "L"
            if next_v != "L" and next_v != "PM": continue
            # Check week max
            wk = d2w[int(dk)]
            week_worked = sum(1 for dn in w2d[wk] if dm.get(str(dn),"L") != "L")
            if week_worked >= WEEK_MAX: continue
            # Shorten rest: assign PM
            dm[dk] = "PM"
            worked += 1

        # Strategy 2: add as 3rd person to PM or AM (adjacent to existing block)
        if worked >= effective_min_work:
            asgn[eid] = dm; continue

        for j in range(len(ds)):
            if worked >= effective_min_work: break
            dk = ds[j]
            if dm.get(dk,"L") != "L": continue
            if int(dk) in vset: continue

            # Check adjacency to PM, AM, or NIGHT block
            # Orden: PM primero (prioridad negocio), luego AM, luego NIGHT.
            # El rebalanceo AM/PM posterior se encarga de redistribuir exceso PM→AM.
            for shift in ["PM", "AM", "NIGHT"]:
                # Count current workers in this shift on this day
                curr_workers = sum(1 for e in rotating
                                   if asgn[str(e["id"])].get(dk,"L") == shift)
                # Coverage max: AM=2, PM=3, NIGHT=2 rotativos always
                allow_max = 2 if shift == "NIGHT" else SHIFT_MAX.get(shift, 3)
                if curr_workers >= allow_max: continue

                # Must be adjacent to same shift
                prev_v = dm.get(ds[j-1],"L") if j>0 else "L"
                next_v = dm.get(ds[j+1],"L") if j+1<len(ds) else "L"
                is_adj = (prev_v == shift or next_v == shift)
                if not is_adj: continue

                # Check block length won't exceed BLOCK_MAX
                # Count consecutive same-shift days around this position
                block_len = 1  # this day
                for dlt in [-1, 1]:
                    k = j + dlt
                    while 0 <= k < len(ds) and dm.get(ds[k],"L") == shift:
                        block_len += 1
                        k += dlt
                bmax = {"PM": 5, "AM": 4, "NIGHT": 3}  # PM max 5 exceptionally
                if block_len > bmax.get(shift, 4): continue

                # No bad transition
                if prev_v != "L" and prev_v != shift: continue
                if next_v != "L" and next_v != shift: continue

                # Week max
                wk = d2w[int(dk)]
                wk_worked = sum(1 for dn in w2d[wk] if dm.get(str(dn),"L") != "L")
                if wk_worked >= WEEK_MAX: continue

                dm[dk] = shift
                worked += 1
                break

        # Strategy 3: shorten post-AM rest from 2 to 1 day
        if worked >= effective_min_work:
            asgn[eid] = dm; continue

        for j in range(1, len(ds)-1):
            if worked >= effective_min_work: break
            dk   = ds[j]
            prev = dm.get(ds[j-1], "L")
            curr = dm.get(dk, "L")
            if prev != "AM" or curr != "L": continue
            if int(dk) in vset: continue
            if j < 2 or dm.get(ds[j-2], "L") == "L": continue
            next_v = dm.get(ds[j+1], "L") if j+1 < len(ds) else "L"
            if next_v != "L" and next_v != "AM": continue
            wk = d2w[int(dk)]
            week_worked = sum(1 for dn in w2d[wk] if dm.get(str(dn),"L") != "L")
            if week_worked >= WEEK_MAX: continue
            dm[dk] = "AM"
            worked += 1

        # Strategy 4: extend any block by 1 day (own employee's block)
        # More aggressive — scans all free days and finds a neighbor of any shift
        if worked >= effective_min_work:
            asgn[eid] = dm; continue

        for j in range(len(ds)):
            if worked >= effective_min_work: break
            dk = ds[j]
            if dm.get(dk,"L") != "L" or int(dk) in vset: continue
            idx_j = j
            for dlt in [-1, 1]:
                ni = idx_j + dlt
                if not (0 <= ni < len(ds)): continue
                nbr_shift = dm.get(ds[ni], "L")
                if nbr_shift == "L": continue
                # Check other side is clear (no bad transition)
                other = idx_j - dlt
                other_v = dm.get(ds[other],"L") if 0<=other<len(ds) else "L"
                if other_v != "L" and other_v != nbr_shift: continue
                # Block max check
                blen = 1
                k = ni
                while 0 <= k < len(ds) and dm.get(ds[k],"L") == nbr_shift:
                    blen += 1; k += dlt
                hard_bmax = {"PM":5,"AM":4,"NIGHT":3}
                if blen > hard_bmax.get(nbr_shift, 4): continue
                # Week max
                wk = d2w[int(dk)]
                wk_w = sum(1 for dn in w2d[wk] if dm.get(str(dn),"L") != "L")
                if wk_w >= WEEK_MAX: continue
                dm[dk] = nbr_shift
                worked += 1
                break

        asgn[eid] = dm

    return asgn


def _enforce_block_max(dm, ds):
    """
    Scan for blocks exceeding maximum length and trim excess from the end.
    PM: max 5 days  (exceptionally, never 6+)
    AM: max 4 days
    NIGHT: max 3 days (strict)
    """
    HARD_MAX_BLOCK = {"PM": 5, "AM": 4, "NIGHT": 3}

    i = 0
    while i < len(ds):
        dk = ds[i]
        shift = dm.get(dk, "L")
        if shift == "L":
            i += 1; continue

        # Find block end
        block_start = i
        while i < len(ds) and dm.get(ds[i], "L") == shift:
            i += 1
        block_len = i - block_start
        max_len   = HARD_MAX_BLOCK.get(shift, 5)

        if block_len > max_len:
            # Trim excess from block end
            for j in range(block_start + max_len, i):
                dm[ds[j]] = "L"

    return dm

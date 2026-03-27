bl_info = {
    "name": "Nsight CSV Importer", "author": "zk", "version": (1, 0, 0),
    "blender": (5, 0, 0), "location": "File > Import > Nsight Graphics CSV",
    "description": "Importador CSV do NVIDIA Nsight Graphics",
    "category": "Import-Export",
}

import bpy, bmesh, csv, math, os, re, struct, time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Annotated, Final, Protocol, TypeAlias, cast
import mathutils
from bpy_extras.io_utils import ImportHelper, axis_conversion
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty,
                       FloatProperty, IntProperty, StringProperty)
from bpy.types import Context, Mesh, Object, Operator, OperatorFileListElement

# ── TYPE ALIASES ──────────────────────────────────────────────────────────────
Vec2:      TypeAlias = tuple[float, float]
Vec3:      TypeAlias = tuple[float, float, float]
Vec4:      TypeAlias = tuple[float, float, float, float]
SafeFloat: TypeAlias = Annotated[float, "sanitized-ieee754"]

_ENCS:     Final[tuple[str, ...]] = ('utf-8-sig', 'utf-16', 'utf-16le', 'latin-1')
_IDX_KEYS: Final[tuple[str, ...]] = ("Index", "VertexID", "IB Offset")
_RESTART:  Final[frozenset[int]]  = frozenset({0xFFFF, 0xFFFFFFFF})
_RE_NUM:   Final[re.Pattern]      = re.compile(
    r'[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?')

# ── IEEE-754 PARSER — FP16 / FP32 / FP64 ─────────────────────────────────────
# Detecção de largura por contagem de nibbles ANTES de normalizar zeros.
# hex bruto: '0x0001' → 4 nibbles → FP16 ; '0x00000001' → 8 nibbles → FP32.
# lstrip('0') é aplicado SOMENTE para conversão numérica, nunca para medir largura.
_FP_MAP: Final[dict[int, tuple[str, int]]] = {4: ('!e', 4), 8: ('!f', 8), 16: ('!d', 16)}

def _f(v: str, as_int: bool = False) -> SafeFloat:
    s = v.strip()
    if not s: return 0.0
    try:
        r = float(s); return 0.0 if not math.isfinite(r) else r
    except ValueError: pass
    lo = s.lower()
    if '0x' in lo:
        try:
            sign = -1.0 if lo.startswith('-') else 1.0
            raw  = lo.lstrip('-').removeprefix('0x')          # preservar zeros à esq.
            if as_int: return float(sign * int(raw or '0', 16))
            nb = len(raw)                                      # nibbles reais — largura bruta
            # Arredondar para a largura padrão imediatamente superior (4/8/16)
            w_key = 4 if nb <= 4 else 8 if nb <= 8 else 16
            fmt, w = _FP_MAP[w_key]
            r = sign * cast(float, struct.unpack(fmt, bytes.fromhex(raw.zfill(w)))[0])
            return 0.0 if not math.isfinite(r) else r
        except (ValueError, struct.error): return 0.0
    if m := _RE_NUM.search(s):
        try:
            r = float(m.group()); return 0.0 if not math.isfinite(r) else r
        except ValueError: pass
    return 0.0

# ── DATA STRUCTURES ───────────────────────────────────────────────────────────
@dataclass(slots=True, frozen=True)
class Config:
    filepath: str;      matrix: mathutils.Matrix
    invert_v: bool;     fix_winding: bool;  topology: str
    merge_doubles: bool
    pos_n: int;  norm_n: int;  uv_n: int
    col_n: int;  tan_n:  int;  skin_n: int
    pfx: dict[str, str]

@dataclass(slots=True)
class Plane:
    vertex_count:  int                       = 0
    verts:    list[list[Vec3]]              = field(default_factory=list)
    normals:  list[list[Vec3]]              = field(default_factory=list)
    uvs:      list[list[Vec2]]              = field(default_factory=list)
    colors:   list[list[Vec4]]              = field(default_factory=list)
    tangents: list[list[Vec3]]              = field(default_factory=list)
    skin_idx: list[list[tuple[int, ...]]]   = field(default_factory=list)
    skin_wgt: list[list[tuple[float, ...]]] = field(default_factory=list)
    faces:    list[tuple[int, ...]]         = field(default_factory=list)
    rows_total:    int = 0
    rows_corrupt:  int = 0
    faces_corrupt: int = 0

# ── PARSER ────────────────────────────────────────────────────────────────────
class Parser:
    __slots__ = ('cfg', 'plane')

    def __init__(self, cfg: Config) -> None:
        self.cfg   = cfg
        self.plane = Plane(
            verts    = [[] for _ in range(cfg.pos_n)],
            normals  = [[] for _ in range(cfg.norm_n)],
            uvs      = [[] for _ in range(cfg.uv_n)],
            colors   = [[] for _ in range(cfg.col_n)],
            tangents = [[] for _ in range(cfg.tan_n)],
            skin_idx = [[] for _ in range(cfg.skin_n)],
            skin_wgt = [[] for _ in range(cfg.skin_n)],
        )

    # Mapa header→índice construído uma única vez O(n); _cols é O(1) por atributo.
    @staticmethod
    def _hmap(hdr: list[str]) -> dict[str, int]:
        return {h: i for i, h in enumerate(hdr)}

    @staticmethod
    def _cols(hmap: dict[str, int], pfx: str, nc: int,
               hdr: list[str]) -> tuple[int | None, ...]:
        return tuple(
            next((hmap[h] for h in hdr if pfx in h and f"Component {c}" in h), None)
            for c in range(nc))

    def _open(self):
        for enc in _ENCS:
            fh = None
            try:
                fh     = open(self.cfg.filepath, 'r', encoding=enc, newline='')
                sample = fh.read(8192); fh.seek(0)
                dial   = csv.Sniffer().sniff(sample[:4096]) if sample else csv.excel
                rdr    = csv.reader(fh, dial)
                hdr    = [h.strip() for h in next(rdr)]
                return fh, rdr, hdr
            except (UnicodeDecodeError, StopIteration, csv.Error):
                if fh: fh.close()
        raise ValueError("CSV: encoding não suportado ou arquivo vazio.")

    def parse(self) -> "Plane":
        fh, rdr, hdr = self._open()
        cfg, p       = self.cfg, self.plane
        hmap         = self._hmap(hdr)

        missing = [v for v in cfg.pfx.values() if v and not any(v in h for h in hdr)]
        if missing:
            fh.close(); raise ValueError(f"Atributos ausentes: {', '.join(missing)}")

        def routes(attr: str, n: int, nc: int) -> list[tuple[int, tuple]]:
            return [(i, c) for i in range(n)
                    if None not in (c := self._cols(hmap, cfg.pfx[f'{attr}_{i}'], nc, hdr))]

        p_rt  = routes('pos',  cfg.pos_n,  3)
        n_rt  = routes('norm', cfg.norm_n, 3)
        uv_rt = routes('uv',   cfg.uv_n,   2)
        c_rt  = routes('col',  cfg.col_n,  4)
        t_rt  = routes('tan',  cfg.tan_n,  3)
        sk_rt = [(i,
                  self._cols(hmap, cfg.pfx[f'skin_idx_{i}'], 4, hdr),
                  self._cols(hmap, cfg.pfx[f'skin_wgt_{i}'], 4, hdr))
                 for i in range(cfg.skin_n)
                 if None not in self._cols(hmap, cfg.pfx[f'skin_idx_{i}'], 4, hdr)]

        idx_col     = next((hmap[h] for h in hdr if any(k in h for k in _IDX_KEYS)), None)
        use_row_idx = idx_col is None
        mW          = cfg.matrix
        mN          = mW.to_3x3().inverted().transposed()
        inv_v       = cfg.invert_v
        vmap: dict[int, int] = {}
        seq:  list[int]      = []

        with fh:
            for ri, row in enumerate(rdr):
                if not row: continue
                p.rows_total += 1
                try:
                    raw = ri if use_row_idx else int(_f(row[idx_col].strip(), True))
                    if raw in _RESTART:
                        seq.append(-1); continue
                    if raw not in vmap:
                        vmap[raw] = len(vmap)
                        for i, (c0, c1, c2) in p_rt:
                            p.verts[i].append(
                                tuple(mW @ mathutils.Vector((_f(row[c0]), _f(row[c1]), _f(row[c2])))))
                        for i, (c0, c1, c2) in n_rt:
                            p.normals[i].append(
                                tuple((mN @ mathutils.Vector(
                                    (_f(row[c0]), _f(row[c1]), _f(row[c2])))).normalized()))
                        for i, (c0, c1) in uv_rt:
                            u, v = _f(row[c0]), _f(row[c1])
                            p.uvs[i].append((u, 1.0 - v if inv_v else v))
                        for i, (c0, c1, c2, c3) in c_rt:
                            p.colors[i].append((
                                _f(row[c0]), _f(row[c1]), _f(row[c2]),
                                _f(row[c3]) if c3 is not None else 1.0))
                        for i, (c0, c1, c2) in t_rt:
                            p.tangents[i].append(
                                tuple((mN @ mathutils.Vector(
                                    (_f(row[c0]), _f(row[c1]), _f(row[c2])))).normalized()))
                        for i, bi, bw in sk_rt:
                            p.skin_idx[i].append(
                                tuple(int(_f(row[c], True)) for c in bi if c is not None))
                            p.skin_wgt[i].append(
                                tuple(_f(row[c]) for c in bw if c is not None))
                    seq.append(vmap[raw])
                except Exception:
                    p.rows_corrupt += 1

        p.vertex_count = len(vmap)
        if not p.vertex_count: raise ValueError("Buffer vazio: nenhum vértice lido.")
        self._build_topology(seq)
        return p

    # ── Topology Engine — Primitive Restart aware (DX/GL GPU dumps 2026) ─────
    def _build_topology(self, idx: list[int]) -> None:
        fw    = self.cfg.fix_winding
        p     = self.plane
        faces: list[tuple[int, ...]] = []

        def tri(a: int, b: int, c: int) -> tuple[int, int, int]:
            return (a, c, b) if fw else (a, b, c)

        if self.cfg.topology == 'LIST':
            for i in range(0, len(idx) - 2, 3):
                a, b, c = idx[i], idx[i+1], idx[i+2]
                if -1 in (a, b, c) or len({a, b, c}) < 3:
                    p.faces_corrupt += 1; continue
                faces.append(tri(a, b, c))

        elif self.cfg.topology in ('STRIP', 'FAN'):
            is_fan = self.cfg.topology == 'FAN'
            s = 0
            for e in range(len(idx) + 1):
                if e < len(idx) and idx[e] != -1: continue
                sub = idx[s:e]; s = e + 1
                if len(sub) < 3: continue
                hub = sub[0]
                for i in range(len(sub) - 2):
                    if is_fan:
                        a, b, c = hub, sub[i+1], sub[i+2]
                    else:
                        a, b, c = sub[i], sub[i+1], sub[i+2]
                        b, c = (c, b) if i % 2 else (b, c)  # winding alternado strip
                    if len({a, b, c}) == 3: faces.append(tri(a, b, c))
                    else: p.faces_corrupt += 1

        p.faces = faces

# ── MESH BUILDER — Blender 5.0 Generic Attributes API ────────────────────────
# foreach_set é o único caminho permitido para injeção de dados no C-Buffer.
class Builder:
    @staticmethod
    def _flat(data: list, stride: int) -> list[float]:
        return [c for v in data for c in (v[:stride] if len(v) >= stride else (*v, *([0.0]*(stride-len(v)))))]

    @staticmethod
    def build(name: str, p: Plane, cfg: Config) -> Object:
        mesh: Mesh = bpy.data.meshes.new(f"{name}_Mesh")
        verts = p.verts[0] if p.verts and p.verts[0] else [(0., 0., 0.)] * p.vertex_count

        # from_pydata shade_flat=False: smooth shading nativo, API Blender 4.1+ / 5.0
        mesh.from_pydata(verts, [], p.faces, shade_flat=False)

        # Posições extras: POINT / FLOAT_VECTOR
        for i in range(1, len(p.verts)):
            if not p.verts[i]: continue
            mesh.attributes.new(f"Position_{i}", 'FLOAT_VECTOR', 'POINT') \
                .data.foreach_set("vector", Builder._flat(p.verts[i], 3))

        # loop→vertex_index reutilizado por todos os canais CORNER
        lc = len(mesh.loops)
        lv = [0] * lc
        mesh.loops.foreach_get("vertex_index", lv)

        # Normais split: atributo genérico CORNER/FLOAT_VECTOR (Blender 5.0 canônico)
        if p.normals and p.normals[0]:
            n0 = p.normals[0]
            mesh.attributes.new("custom_normal", 'FLOAT_VECTOR', 'CORNER') \
                .data.foreach_set("vector", [c for vi in lv for c in n0[vi]])
        for i in range(1, len(p.normals)):
            if not p.normals[i]: continue
            mesh.attributes.new(f"Normal_{i}", 'FLOAT_VECTOR', 'POINT') \
                .data.foreach_set("vector", Builder._flat(p.normals[i], 3))

        # UVs: CORNER / FLOAT2
        for i, uvl in enumerate(p.uvs):
            if not uvl: continue
            mesh.attributes.new(f"TEXCOORD_{i}", 'FLOAT2', 'CORNER') \
                .data.foreach_set("vector", [c for vi in lv for c in uvl[vi]])

        # Vertex Colors: CORNER / FLOAT_COLOR (RGBA 32-bit linear)
        for i, cl in enumerate(p.colors):
            if not cl: continue
            mesh.attributes.new(f"VertexColor_{i}", 'FLOAT_COLOR', 'CORNER') \
                .data.foreach_set("color", [c for vi in lv for c in cl[vi]])

        # Tangentes: CORNER / FLOAT_VECTOR (xyz; w descartado)
        for i, tl in enumerate(p.tangents):
            if not tl: continue
            mesh.attributes.new(f"Tangent_{i}", 'FLOAT_VECTOR', 'CORNER') \
                .data.foreach_set("vector", [c for vi in lv for c in tl[vi][:3]])

        mesh.validate(); mesh.update()

        if cfg.merge_doubles:
            bm = bmesh.new(); bm.from_mesh(mesh)
            bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-4)
            bm.to_mesh(mesh); bm.free(); mesh.update()

        obj = bpy.data.objects.new(name, mesh)
        bpy.context.collection.objects.link(obj)

        # Skinning: peso FLOAT / POINT + vertex_groups espelhados
        if p.skin_idx and p.skin_wgt:
            bw: defaultdict[int, list[float]] = defaultdict(lambda: [0.0] * p.vertex_count)
            for s in range(cfg.skin_n):
                if not p.skin_idx[s]: continue
                for vi, (idxs, wgts) in enumerate(zip(p.skin_idx[s], p.skin_wgt[s])):
                    for bi, w in zip(idxs, wgts):
                        if w > 1e-4: bw[bi][vi] = w
            for bi, wa in bw.items():
                grp = f"Bone_{bi}"
                mesh.attributes.new(grp, 'FLOAT', 'POINT').data.foreach_set("value", wa)
                obj.vertex_groups.new(name=grp)

        return obj

# ── OPERATOR & UI ─────────────────────────────────────────────────────────────
_AX: Final = [('X','X',''),('Y','Y',''),('Z','Z',''),('-X','-X',''),('-Y','-Y',''),('-Z','-Z','')]

def _axis_upd(self: "IMPORT_OT_nsight_csv", _: Context) -> None:
    if self.forward.lstrip('-') == self.up.lstrip('-'):
        self.up = next(a[0] for a in _AX
                       if a[0].lstrip('-') not in (self.forward.lstrip('-'), self.up.lstrip('-')))

class IMPORT_OT_nsight_csv(Operator, ImportHelper):
    """Importa dumps CSV do NVIDIA Nsight Graphics 2026 — Blender 5.0"""
    bl_idname  = "import_scene.nsight_csv"
    bl_label   = "Nsight Graphics CSV"
    bl_options = {'PRESET', 'UNDO'}

    filename_ext = ".csv"
    filter_glob: StringProperty(default="*.csv", options={'HIDDEN'}, maxlen=255)   # type: ignore
    files:       CollectionProperty(name="Arquivos", type=OperatorFileListElement)  # type: ignore
    directory:   StringProperty(subtype='DIR_PATH')                                 # type: ignore

    scale:   FloatProperty(name="Escala",       default=1.0,  min=1e-4)                       # type: ignore
    forward: EnumProperty (name="Eixo Forward", default='-Y', items=_AX, update=_axis_upd)    # type: ignore
    up:      EnumProperty (name="Eixo Up",      default='Z',  items=_AX, update=_axis_upd)    # type: ignore

    topology:      EnumProperty(name="Topologia", default='LIST', items=[                      # type: ignore
        ('LIST','Triangle List',''),('STRIP','Triangle Strip',''),('FAN','Triangle Fan','')])
    fix_winding:   BoolProperty(name="Corrigir Winding (CW→CCW)", default=True)               # type: ignore
    invert_v:      BoolProperty(name="Inverter V (DX→OpenGL)",    default=True)               # type: ignore
    merge_doubles: BoolProperty(name="Merge Doubles",              default=False)              # type: ignore

    pos_n:  IntProperty(name="Posições",   default=1, min=0, max=4)                           # type: ignore
    norm_n: IntProperty(name="Normais",    default=1, min=0, max=4)                           # type: ignore
    uv_n:   IntProperty(name="UVs",        default=1, min=0, max=4)                           # type: ignore
    col_n:  IntProperty(name="Cores",      default=0, min=0, max=4)                           # type: ignore
    tan_n:  IntProperty(name="Tangentes",  default=0, min=0, max=4)                           # type: ignore
    skin_n: IntProperty(name="Skin Slots", default=0, min=0, max=4)                           # type: ignore

    pos_0:  StringProperty(name="Pos 0",      default="POSITION0")     # type: ignore
    pos_1:  StringProperty(name="Pos 1",      default="POSITION1")     # type: ignore
    pos_2:  StringProperty(name="Pos 2",      default="POSITION2")     # type: ignore
    pos_3:  StringProperty(name="Pos 3",      default="POSITION3")     # type: ignore
    norm_0: StringProperty(name="Norm 0",     default="NORMAL0")       # type: ignore
    norm_1: StringProperty(name="Norm 1",     default="NORMAL1")       # type: ignore
    norm_2: StringProperty(name="Norm 2",     default="NORMAL2")       # type: ignore
    norm_3: StringProperty(name="Norm 3",     default="NORMAL3")       # type: ignore
    uv_0:   StringProperty(name="UV 0",       default="TEXCOORD0")     # type: ignore
    uv_1:   StringProperty(name="UV 1",       default="TEXCOORD1")     # type: ignore
    uv_2:   StringProperty(name="UV 2",       default="TEXCOORD2")     # type: ignore
    uv_3:   StringProperty(name="UV 3",       default="TEXCOORD3")     # type: ignore
    col_0:  StringProperty(name="Color 0",    default="COLOR0")        # type: ignore
    col_1:  StringProperty(name="Color 1",    default="COLOR1")        # type: ignore
    col_2:  StringProperty(name="Color 2",    default="COLOR2")        # type: ignore
    col_3:  StringProperty(name="Color 3",    default="COLOR3")        # type: ignore
    tan_0:  StringProperty(name="Tan 0",      default="TANGENT0")      # type: ignore
    tan_1:  StringProperty(name="Tan 1",      default="TANGENT1")      # type: ignore
    tan_2:  StringProperty(name="Tan 2",      default="TANGENT2")      # type: ignore
    tan_3:  StringProperty(name="Tan 3",      default="TANGENT3")      # type: ignore
    skin_idx_0: StringProperty(name="BlendIdx 0", default="BLENDINDICES0")  # type: ignore
    skin_wgt_0: StringProperty(name="BlendWgt 0", default="BLENDWEIGHT0")   # type: ignore
    skin_idx_1: StringProperty(name="BlendIdx 1", default="BLENDINDICES1")  # type: ignore
    skin_wgt_1: StringProperty(name="BlendWgt 1", default="BLENDWEIGHT1")   # type: ignore
    skin_idx_2: StringProperty(name="BlendIdx 2", default="BLENDINDICES2")  # type: ignore
    skin_wgt_2: StringProperty(name="BlendWgt 2", default="BLENDWEIGHT2")   # type: ignore
    skin_idx_3: StringProperty(name="BlendIdx 3", default="BLENDINDICES3")  # type: ignore
    skin_wgt_3: StringProperty(name="BlendWgt 3", default="BLENDWEIGHT3")   # type: ignore

    def draw(self, _: Context) -> None:
        L = self.layout
        L.use_property_split    = True
        L.use_property_decorate = False

        b = L.box(); b.label(text="Transformação", icon='EMPTY_AXIS')
        c = b.column(align=True)
        c.prop(self, "scale"); c.prop(self, "forward"); c.prop(self, "up")

        b = L.box(); b.label(text="Malha & Topologia", icon='MESH_DATA')
        c = b.column(align=True)
        c.prop(self, "topology"); c.separator()
        c.prop(self, "fix_winding"); c.prop(self, "invert_v"); c.prop(self, "merge_doubles")

        def _slots(label: str, base: str, icon: str) -> None:
            b = L.box(); b.label(text=label, icon=icon)
            b.prop(self, f"{base}_n", text="Slots Ativos")
            if n := getattr(self, f"{base}_n"):
                c = b.column(align=True)
                for i in range(n): c.prop(self, f"{base}_{i}")

        _slots("Posições (XYZ)",       "pos",  'VERTEXSEL')
        _slots("Normais (XYZ)",        "norm", 'NORMALS_VERTEX')
        _slots("Coordenadas UV",       "uv",   'UV')
        _slots("Vertex Colors (RGBA)", "col",  'COLOR')
        _slots("Tangentes (XYZ)",      "tan",  'CURVE_PATH')

        b = L.box(); b.label(text="Armature Skinning", icon='BONE_DATA')
        b.prop(self, "skin_n", text="Slots de Skin")
        if self.skin_n:
            c = b.column(align=True)
            for i in range(self.skin_n):
                r = c.row(align=True)
                r.prop(self, f"skin_idx_{i}", text=f"Idx {i}")
                r.prop(self, f"skin_wgt_{i}", text=f"Wgt {i}")

    def execute(self, context: Context):
        t0 = time.perf_counter()
        ok = err = total_cr = total_cf = 0
        pfx: dict[str, str] = {
            f"{a}_{i}": getattr(self, f"{a}_{i}")
            for a in ('pos', 'norm', 'uv', 'col', 'tan')
            for i in range(getattr(self, f"{a}_n"))
        }
        for i in range(self.skin_n):
            pfx[f"skin_idx_{i}"] = getattr(self, f"skin_idx_{i}")
            pfx[f"skin_wgt_{i}"] = getattr(self, f"skin_wgt_{i}")

        mW = (mathutils.Matrix.Scale(self.scale, 4) @
              axis_conversion(from_forward=self.forward, from_up=self.up).to_4x4())

        for fe in self.files:
            fp   = os.path.join(self.directory, fe.name)
            name = os.path.splitext(fe.name)[0]
            cfg  = Config(
                filepath=fp, matrix=mW, invert_v=self.invert_v,
                fix_winding=self.fix_winding, topology=self.topology,
                merge_doubles=self.merge_doubles,
                pos_n=self.pos_n,  norm_n=self.norm_n, uv_n=self.uv_n,
                col_n=self.col_n,  tan_n=self.tan_n,   skin_n=self.skin_n,
                pfx=pfx,
            )
            try:
                p = Parser(cfg).parse()
                total_cr += p.rows_corrupt; total_cf += p.faces_corrupt
                Builder.build(name, p, cfg); ok += 1
                if p.rows_corrupt or p.faces_corrupt:
                    self.report({'WARNING'},
                        f"[{name}] I/O Parcial — "
                        f"{p.rows_total - p.rows_corrupt}/{p.rows_total} linhas ok, "
                        f"{p.faces_corrupt} faces descartadas.")
            except Exception as e:
                self.report({'ERROR'}, f"[{name}] {e}"); err += 1

        lvl = 'WARNING' if (err or total_cr or total_cf) else 'INFO'
        self.report({lvl},
            f"{ok} importado(s), {err} erro(s) | "
            f"Blindado: {total_cr} linhas / {total_cf} faces "
            f"— {time.perf_counter() - t0:.3f}s")
        return {'FINISHED'}


def _menu(self, _): self.layout.operator(IMPORT_OT_nsight_csv.bl_idname,
                                         text="Nsight Graphics CSV (.csv)")

def register():
    bpy.utils.register_class(IMPORT_OT_nsight_csv)
    bpy.types.TOPBAR_MT_file_import.append(_menu)

def unregister():
    bpy.utils.unregister_class(IMPORT_OT_nsight_csv)
    bpy.types.TOPBAR_MT_file_import.remove(_menu)

if __name__ == "__main__":
    register()

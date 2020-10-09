"""
colorLib.builder: Build COLR/CPAL tables from scratch

"""
import collections
import copy
import enum
from functools import partial
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Type,
    TypeVar,
    Union,
)
from fontTools.ttLib.tables import C_O_L_R_
from fontTools.ttLib.tables import C_P_A_L_
from fontTools.ttLib.tables import _n_a_m_e
from fontTools.ttLib.tables import otTables as ot
from fontTools.ttLib.tables.otTables import (
    ExtendMode,
    CompositeMode,
    VariableValue,
    VariableFloat,
    VariableInt,
)
from .errors import ColorLibError


# TODO move type aliases to colorLib.types?
T = TypeVar("T")
_Kwargs = Mapping[str, Any]
_PaintInput = Union[int, _Kwargs, ot.Paint, Tuple[str, "_PaintInput"]]
_PaintInputList = Sequence[_PaintInput]
_ColorGlyphsDict = Dict[str, Union[_PaintInputList, ot.LayerV1List]]
_ColorGlyphsV0Dict = Dict[str, Sequence[Tuple[str, int]]]
_Number = Union[int, float]
_ScalarInput = Union[_Number, VariableValue, Tuple[_Number, int]]
_ColorStopTuple = Tuple[_ScalarInput, int]
_ColorStopInput = Union[_ColorStopTuple, _Kwargs, ot.ColorStop]
_ColorStopsList = Sequence[_ColorStopInput]
_ExtendInput = Union[int, str, ExtendMode]
_CompositeInput = Union[int, str, CompositeMode]
_ColorLineInput = Union[_Kwargs, ot.ColorLine]
_PointTuple = Tuple[_ScalarInput, _ScalarInput]
_AffineTuple = Tuple[
    _ScalarInput, _ScalarInput, _ScalarInput, _ScalarInput, _ScalarInput, _ScalarInput
]
_AffineInput = Union[_AffineTuple, ot.Affine2x3]


def populateCOLRv0(
    table: ot.COLR,
    colorGlyphsV0: _ColorGlyphsV0Dict,
    glyphMap: Optional[Mapping[str, int]] = None,
):
    """Build v0 color layers and add to existing COLR table.

    Args:
        table: a raw otTables.COLR() object (not ttLib's table_C_O_L_R_).
        colorGlyphsV0: map of base glyph names to lists of (layer glyph names,
            color palette index) tuples.
        glyphMap: a map from glyph names to glyph indices, as returned from
            TTFont.getReverseGlyphMap(), to optionally sort base records by GID.
    """
    if glyphMap is not None:
        colorGlyphItems = sorted(
            colorGlyphsV0.items(), key=lambda item: glyphMap[item[0]]
        )
    else:
        colorGlyphItems = colorGlyphsV0.items()
    baseGlyphRecords = []
    layerRecords = []
    for baseGlyph, layers in colorGlyphItems:
        baseRec = ot.BaseGlyphRecord()
        baseRec.BaseGlyph = baseGlyph
        baseRec.FirstLayerIndex = len(layerRecords)
        baseRec.NumLayers = len(layers)
        baseGlyphRecords.append(baseRec)

        for layerGlyph, paletteIndex in layers:
            layerRec = ot.LayerRecord()
            layerRec.LayerGlyph = layerGlyph
            layerRec.PaletteIndex = paletteIndex
            layerRecords.append(layerRec)

    table.BaseGlyphRecordCount = len(baseGlyphRecords)
    table.BaseGlyphRecordArray = ot.BaseGlyphRecordArray()
    table.BaseGlyphRecordArray.BaseGlyphRecord = baseGlyphRecords
    table.LayerRecordArray = ot.LayerRecordArray()
    table.LayerRecordArray.LayerRecord = layerRecords
    table.LayerRecordCount = len(layerRecords)


def buildCOLR(
    colorGlyphs: _ColorGlyphsDict,
    version: Optional[int] = None,
    glyphMap: Optional[Mapping[str, int]] = None,
    varStore: Optional[ot.VarStore] = None,
) -> C_O_L_R_.table_C_O_L_R_:
    """Build COLR table from color layers mapping.

    Args:
        colorGlyphs: map of base glyph name to, either list of (layer glyph name,
            color palette index) tuples for COLRv0; or list of Paints (dicts)
            for COLRv1.
        version: the version of COLR table. If None, the version is determined
            by the presence of COLRv1 paints or variation data (varStore), which
            require version 1; otherwise, if all base glyphs use only simple color
            layers, version 0 is used.
        glyphMap: a map from glyph names to glyph indices, as returned from
            TTFont.getReverseGlyphMap(), to optionally sort base records by GID.
        varStore: Optional ItemVarationStore for deltas associated with v1 layer.

    Return:
        A new COLR table.
    """
    self = C_O_L_R_.table_C_O_L_R_()

    if varStore is not None and version == 0:
        raise ValueError("Can't add VarStore to COLRv0")

    if version in (None, 0) and not varStore:
        # split color glyphs into v0 and v1 and encode separately
        colorGlyphsV0, colorGlyphsV1 = _split_color_glyphs_by_version(colorGlyphs)
        if version == 0 and colorGlyphsV1:
            raise ValueError("Can't encode COLRv1 glyphs in COLRv0")
    else:
        # unless explicitly requested for v1 or have variations, in which case
        # we encode all color glyph as v1
        colorGlyphsV0, colorGlyphsV1 = None, colorGlyphs

    colr = ot.COLR()

    if colorGlyphsV0:
        populateCOLRv0(colr, colorGlyphsV0, glyphMap)
    else:
        colr.BaseGlyphRecordCount = colr.LayerRecordCount = 0
        colr.BaseGlyphRecordArray = colr.LayerRecordArray = None

    if colorGlyphsV1:
        colr.BaseGlyphV1List = buildBaseGlyphV1List(colorGlyphsV1, glyphMap)

    if version is None:
        version = 1 if (varStore or colorGlyphsV1) else 0
    elif version not in (0, 1):
        raise NotImplementedError(version)
    self.version = colr.Version = version

    if version == 0:
        self._fromOTTable(colr)
    else:
        colr.VarStore = varStore
        self.table = colr

    return self


class ColorPaletteType(enum.IntFlag):
    USABLE_WITH_LIGHT_BACKGROUND = 0x0001
    USABLE_WITH_DARK_BACKGROUND = 0x0002

    @classmethod
    def _missing_(cls, value):
        # enforce reserved bits
        if isinstance(value, int) and (value < 0 or value & 0xFFFC != 0):
            raise ValueError(f"{value} is not a valid {cls.__name__}")
        return super()._missing_(value)


# None, 'abc' or {'en': 'abc', 'de': 'xyz'}
_OptionalLocalizedString = Union[None, str, Dict[str, str]]


def buildPaletteLabels(
    labels: Iterable[_OptionalLocalizedString], nameTable: _n_a_m_e.table__n_a_m_e
) -> List[Optional[int]]:
    return [
        nameTable.addMultilingualName(l, mac=False)
        if isinstance(l, dict)
        else C_P_A_L_.table_C_P_A_L_.NO_NAME_ID
        if l is None
        else nameTable.addMultilingualName({"en": l}, mac=False)
        for l in labels
    ]


def buildCPAL(
    palettes: Sequence[Sequence[Tuple[float, float, float, float]]],
    paletteTypes: Optional[Sequence[ColorPaletteType]] = None,
    paletteLabels: Optional[Sequence[_OptionalLocalizedString]] = None,
    paletteEntryLabels: Optional[Sequence[_OptionalLocalizedString]] = None,
    nameTable: Optional[_n_a_m_e.table__n_a_m_e] = None,
) -> C_P_A_L_.table_C_P_A_L_:
    """Build CPAL table from list of color palettes.

    Args:
        palettes: list of lists of colors encoded as tuples of (R, G, B, A) floats
            in the range [0..1].
        paletteTypes: optional list of ColorPaletteType, one for each palette.
        paletteLabels: optional list of palette labels. Each lable can be either:
            None (no label), a string (for for default English labels), or a
            localized string (as a dict keyed with BCP47 language codes).
        paletteEntryLabels: optional list of palette entry labels, one for each
            palette entry (see paletteLabels).
        nameTable: optional name table where to store palette and palette entry
            labels. Required if either paletteLabels or paletteEntryLabels is set.

    Return:
        A new CPAL v0 or v1 table, if custom palette types or labels are specified.
    """
    if len({len(p) for p in palettes}) != 1:
        raise ColorLibError("color palettes have different lengths")

    if (paletteLabels or paletteEntryLabels) and not nameTable:
        raise TypeError(
            "nameTable is required if palette or palette entries have labels"
        )

    cpal = C_P_A_L_.table_C_P_A_L_()
    cpal.numPaletteEntries = len(palettes[0])

    cpal.palettes = []
    for i, palette in enumerate(palettes):
        colors = []
        for j, color in enumerate(palette):
            if not isinstance(color, tuple) or len(color) != 4:
                raise ColorLibError(
                    f"In palette[{i}][{j}]: expected (R, G, B, A) tuple, got {color!r}"
                )
            if any(v > 1 or v < 0 for v in color):
                raise ColorLibError(
                    f"palette[{i}][{j}] has invalid out-of-range [0..1] color: {color!r}"
                )
            # input colors are RGBA, CPAL encodes them as BGRA
            red, green, blue, alpha = color
            colors.append(
                C_P_A_L_.Color(*(round(v * 255) for v in (blue, green, red, alpha)))
            )
        cpal.palettes.append(colors)

    if any(v is not None for v in (paletteTypes, paletteLabels, paletteEntryLabels)):
        cpal.version = 1

        if paletteTypes is not None:
            if len(paletteTypes) != len(palettes):
                raise ColorLibError(
                    f"Expected {len(palettes)} paletteTypes, got {len(paletteTypes)}"
                )
            cpal.paletteTypes = [ColorPaletteType(t).value for t in paletteTypes]
        else:
            cpal.paletteTypes = [C_P_A_L_.table_C_P_A_L_.DEFAULT_PALETTE_TYPE] * len(
                palettes
            )

        if paletteLabels is not None:
            if len(paletteLabels) != len(palettes):
                raise ColorLibError(
                    f"Expected {len(palettes)} paletteLabels, got {len(paletteLabels)}"
                )
            cpal.paletteLabels = buildPaletteLabels(paletteLabels, nameTable)
        else:
            cpal.paletteLabels = [C_P_A_L_.table_C_P_A_L_.NO_NAME_ID] * len(palettes)

        if paletteEntryLabels is not None:
            if len(paletteEntryLabels) != cpal.numPaletteEntries:
                raise ColorLibError(
                    f"Expected {cpal.numPaletteEntries} paletteEntryLabels, "
                    f"got {len(paletteEntryLabels)}"
                )
            cpal.paletteEntryLabels = buildPaletteLabels(paletteEntryLabels, nameTable)
        else:
            cpal.paletteEntryLabels = [
                C_P_A_L_.table_C_P_A_L_.NO_NAME_ID
            ] * cpal.numPaletteEntries
    else:
        cpal.version = 0

    return cpal


# COLR v1 tables
# See draft proposal at: https://github.com/googlefonts/colr-gradients-spec

_DEFAULT_ALPHA = VariableFloat(1.0)


def _split_color_glyphs_by_version(
    colorGlyphs: _ColorGlyphsDict,
) -> Tuple[_ColorGlyphsV0Dict, _ColorGlyphsDict]:
    colorGlyphsV0 = {}
    colorGlyphsV1 = {}
    for baseGlyph, layers in colorGlyphs.items():
        if all(isinstance(l, tuple) and isinstance(l[1], int) for l in layers):
            colorGlyphsV0[baseGlyph] = layers
        else:
            colorGlyphsV1[baseGlyph] = layers

    # sanity check
    assert set(colorGlyphs) == (set(colorGlyphsV0) | set(colorGlyphsV1))

    return colorGlyphsV0, colorGlyphsV1


def _to_variable_value(value: _ScalarInput, cls=VariableValue) -> VariableValue:
    if isinstance(value, cls):
        return value
    try:
        it = iter(value)
    except TypeError:  # not iterable
        return cls(value)
    else:
        return cls._make(it)


_to_variable_float = partial(_to_variable_value, cls=VariableFloat)
_to_variable_int = partial(_to_variable_value, cls=VariableInt)


def buildColorIndex(
    paletteIndex: int, alpha: _ScalarInput = _DEFAULT_ALPHA
) -> ot.ColorIndex:
    self = ot.ColorIndex()
    self.PaletteIndex = int(paletteIndex)
    self.Alpha = _to_variable_float(alpha)
    return self


def buildSolidColorPaint(
    paletteIndex: int, alpha: _ScalarInput = _DEFAULT_ALPHA
) -> ot.Paint:
    self = ot.Paint()
    self.Format = 1
    self.Color = buildColorIndex(paletteIndex, alpha)
    return self


def buildColorStop(
    offset: _ScalarInput,
    paletteIndex: int,
    alpha: _ScalarInput = _DEFAULT_ALPHA,
) -> ot.ColorStop:
    self = ot.ColorStop()
    self.StopOffset = _to_variable_float(offset)
    self.Color = buildColorIndex(paletteIndex, alpha)
    return self


def _to_enum_value(v: Union[str, int, T], enumClass: Type[T]) -> T:
    if isinstance(v, enumClass):
        return v
    elif isinstance(v, str):
        try:
            return getattr(enumClass, v.upper())
        except AttributeError:
            raise ValueError(f"{v!r} is not a valid {enumClass.__name__}")
    return enumClass(v)


def _to_extend_mode(v: _ExtendInput) -> ExtendMode:
    return _to_enum_value(v, ExtendMode)


def _to_composite_mode(v: _CompositeInput) -> CompositeMode:
    return _to_enum_value(v, CompositeMode)


def buildColorLine(
    stops: _ColorStopsList, extend: _ExtendInput = ExtendMode.PAD
) -> ot.ColorLine:
    self = ot.ColorLine()
    self.Extend = _to_extend_mode(extend)
    self.StopCount = len(stops)
    self.ColorStop = [
        stop
        if isinstance(stop, ot.ColorStop)
        else buildColorStop(**stop)
        if isinstance(stop, collections.abc.Mapping)
        else buildColorStop(*stop)
        for stop in stops
    ]
    return self


def _to_color_line(obj):
    if isinstance(obj, ot.ColorLine):
        return obj
    elif isinstance(obj, collections.abc.Mapping):
        return buildColorLine(**obj)
    raise TypeError(obj)


def buildLinearGradientPaint(
    colorLine: _ColorLineInput,
    p0: _PointTuple,
    p1: _PointTuple,
    p2: Optional[_PointTuple] = None,
) -> ot.Paint:
    self = ot.Paint()
    self.Format = 2
    self.ColorLine = _to_color_line(colorLine)

    if p2 is None:
        p2 = copy.copy(p1)
    for i, (x, y) in enumerate((p0, p1, p2)):
        setattr(self, f"x{i}", _to_variable_int(x))
        setattr(self, f"y{i}", _to_variable_int(y))

    return self


def buildAffine2x3(
    xx: _ScalarInput,
    xy: _ScalarInput,
    yx: _ScalarInput,
    yy: _ScalarInput,
    dx: _ScalarInput,
    dy: _ScalarInput,
) -> ot.Affine2x3:
    self = ot.Affine2x3()
    locs = locals()
    for attr in ("xx", "xy", "yx", "yy", "dx", "dy"):
        value = locs[attr]
        setattr(self, attr, _to_variable_float(value))
    return self


def buildRadialGradientPaint(
    colorLine: _ColorLineInput,
    c0: _PointTuple,
    c1: _PointTuple,
    r0: _ScalarInput,
    r1: _ScalarInput,
) -> ot.Paint:

    self = ot.Paint()
    self.Format = 3
    self.ColorLine = _to_color_line(colorLine)

    for i, (x, y), r in [(0, c0, r0), (1, c1, r1)]:
        setattr(self, f"x{i}", _to_variable_int(x))
        setattr(self, f"y{i}", _to_variable_int(y))
        setattr(self, f"r{i}", _to_variable_int(r))

    return self


def buildPaintGlyph(glyph: str, paint: _PaintInput) -> ot.Paint:
    self = ot.Paint()
    self.Format = 4
    self.Glyph = glyph
    self.Paint = buildPaint(paint)
    return self


def buildPaintColorGlyph(glyph: str) -> ot.Paint:
    self = ot.Paint()
    self.Format = 5
    self.Glyph = glyph
    return self


def buildPaintTransform(transform: _AffineInput, paint: _PaintInput) -> ot.Paint:
    self = ot.Paint()
    self.Format = 6
    if not isinstance(transform, ot.Affine2x3):
        transform = buildAffine2x3(*transform)
    self.Transform = transform
    self.Paint = buildPaint(paint)
    return self


def buildPaintComposite(
    mode: _CompositeInput, source: _PaintInput, backdrop: _PaintInput
):
    self = ot.Paint()
    self.Format = 7
    self.SourcePaint = buildPaint(source)
    self.CompositeMode = _to_composite_mode(mode)
    self.BackdropPaint = buildPaint(backdrop)
    return self


_PAINT_BUILDERS = {
    1: buildSolidColorPaint,
    2: buildLinearGradientPaint,
    3: buildRadialGradientPaint,
    4: buildPaintGlyph,
    5: buildPaintColorGlyph,
    6: buildPaintTransform,
    7: buildPaintComposite,
}


def buildPaint(paint: _PaintInput) -> ot.Paint:
    if isinstance(paint, ot.Paint):
        return paint
    elif isinstance(paint, int):
        paletteIndex = paint
        return buildSolidColorPaint(paletteIndex)
    elif isinstance(paint, tuple):
        layerGlyph, paint = paint
        return buildPaintGlyph(layerGlyph, paint)
    elif isinstance(paint, collections.abc.Mapping):
        kwargs = dict(paint)
        fmt = kwargs.pop("format")
        try:
            return _PAINT_BUILDERS[fmt](**kwargs)
        except KeyError:
            raise NotImplementedError(fmt)
    raise TypeError(
        f"expected int, Mapping or ot.Paint, found {type(paint).__name__}: {paint!r}"
    )


def buildLayerV1List(layers: _PaintInputList) -> ot.LayerV1List:
    self = ot.LayerV1List()
    self.LayerCount = len(layers)
    self.Paint = [buildPaint(layer) for layer in layers]
    return self


def buildBaseGlyphV1Record(
    baseGlyph: str, layers: Union[_PaintInputList, ot.LayerV1List]
) -> ot.BaseGlyphV1List:
    self = ot.BaseGlyphV1Record()
    self.BaseGlyph = baseGlyph
    if not isinstance(layers, ot.LayerV1List):
        layers = buildLayerV1List(layers)
    self.LayerV1List = layers
    return self


def buildBaseGlyphV1List(
    colorGlyphs: _ColorGlyphsDict,
    glyphMap: Optional[Mapping[str, int]] = None,
) -> ot.BaseGlyphV1List:
    if glyphMap is not None:
        colorGlyphItems = sorted(
            colorGlyphs.items(), key=lambda item: glyphMap[item[0]]
        )
    else:
        colorGlyphItems = colorGlyphs.items()
    records = [
        buildBaseGlyphV1Record(baseGlyph, layers)
        for baseGlyph, layers in colorGlyphItems
    ]
    self = ot.BaseGlyphV1List()
    self.BaseGlyphCount = len(records)
    self.BaseGlyphV1Record = records
    return self

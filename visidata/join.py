import collections
import itertools
import functools
from copy import copy

from visidata import asyncthread, Progress, status, fail, error
from visidata import ColumnItem, ColumnExpr, SubrowColumn, Sheet, Column
from visidata import SheetsSheet

SheetsSheet.addCommand('&', 'join-sheets', 'vd.replace(createJoinedSheet(selectedRows or fail("no sheets selected to join"), jointype=chooseOne(jointypes)))')
Sheet.addCommand('&', 'append-sheets-top2', 'vd.push(createJoinedSheet(vd.sheets[:2], jointype="append"))')
Sheet.addCommand('g&', 'append-sheets-all', 'vd.push(createJoinedSheet(vd.sheets, jointype="append"))')

def createJoinedSheet(sheets, jointype=''):
    sheets[1:] or error("join requires more than 1 sheet")

    if jointype == 'append':
        keyedcols = collections.defaultdict(list, {col.name:[col] for col in sheets[0].visibleCols})
        for s in sheets[1:] or fail('need to join more than one sheet'):
            for col in s.visibleCols:
                key = col.name if col.name in keyedcols else col.sheet.visibleCols.index(col)
                keyedcols[key].append(col)

        return SheetConcat('&'.join(vs.name for vs in sheets), sourceCols=list(keyedcols.values()))

    elif jointype == 'extend':
        vs = copy(sheets[0])
        vs.name = '+'.join(vs.name for vs in sheets)
        vs.reload = functools.partial(ExtendedSheet_reload, vs, sheets)
        vs.rows = tuple()  # to induce reload on first push, see vdtui
        return vs
    else:
        return SheetJoin('+'.join(vs.name for vs in sheets), sources=sheets, jointype=jointype)

jointypes = {k:k for k in ["inner", "outer", "full", "diff", "append", "extend"]}

def joinkey(sheet, row):
    return tuple(c.getDisplayValue(row) for c in sheet.keyCols)


def groupRowsByKey(sheets, rowsBySheetKey, rowsByKey):
    with Progress(gerund='grouping', total=sum(len(vs.rows) for vs in sheets)*2) as prog:
        for vs in sheets:
            # tally rows by keys for each sheet
            rowsBySheetKey[vs] = collections.defaultdict(list)
            for r in vs.rows:
                prog.addProgress(1)
                key = joinkey(vs, r)
                rowsBySheetKey[vs][key].append(r)

        for vs in sheets:
            for r in vs.rows:
                prog.addProgress(1)
                key = joinkey(vs, r)
                if key not in rowsByKey: # gather for this key has not been done yet
                    # multiplicative for non-unique keys
                    rowsByKey[key] = []
                    for crow in itertools.product(*[rowsBySheetKey[vs2].get(key, [None]) for vs2 in sheets]):
                        rowsByKey[key].append([key] + list(crow))


#### slicing and dicing
# rowdef: [(key, ...), sheet1_row, sheet2_row, ...]
#   if a sheet does not have this key, sheet#_row is None
class SheetJoin(Sheet):
    'Column-wise join/merge. `jointype` constructor arg should be one of jointypes.'

    @asyncthread
    def reload(self):
        sheets = self.sources

        # first item in joined row is the key tuple from the first sheet.
        # first columns are the key columns from the first sheet, using its row (0)
        self.columns = []
        for i, c in enumerate(sheets[0].keyCols):
            self.addColumn(SubrowColumn(c.name, ColumnItem(c.name, i, type=c.type, width=c.width), 0))
        self.setKeys(self.columns)

        for sheetnum, vs in enumerate(sheets):
            # subsequent elements are the rows from each source, in order of the source sheets
            ctr = collections.Counter(c.name for c in vs.nonKeyVisibleCols)
            for c in vs.nonKeyVisibleCols:
                newname = c.name if ctr[c.name] == 1 else '%s_%s' % (vs.name, c.name)
                self.addColumn(SubrowColumn(newname, c, sheetnum+1))

        rowsBySheetKey = {}
        rowsByKey = {}

        groupRowsByKey(sheets, rowsBySheetKey, rowsByKey)

        self.rows = []

        with Progress(gerund='joining', total=len(rowsByKey)) as prog:
            for k, combinedRows in rowsByKey.items():
                prog.addProgress(1)

                if self.jointype == 'full':  # keep all rows from all sheets
                    for combinedRow in combinedRows:
                        self.addRow(combinedRow)

                elif self.jointype == 'inner':  # only rows with matching key on all sheets
                    for combinedRow in combinedRows:
                        if all(combinedRow):
                            self.addRow(combinedRow)

                elif self.jointype == 'outer':  # all rows from first sheet
                    for combinedRow in combinedRows:
                        if combinedRow[1]:
                            self.addRow(combinedRow)

                elif self.jointype == 'diff':  # only rows without matching key on all sheets
                    for combinedRow in combinedRows:
                        if not all(combinedRow):
                            self.addRow(combinedRow)


## for ExtendedSheet_reload below
class ExtendedColumn(Column):
    def calcValue(self, row):
        key = joinkey(self.sheet.joinSources[0], row)
        srcsheet = self.sheet.joinSources[self.sheetnum]
        srcrow = self.sheet.rowsBySheetKey[srcsheet][key]
        if srcrow[0]:
            return self.sourceCol.calcValue(srcrow[0])


@asyncthread
def ExtendedSheet_reload(self, sheets):
    self.joinSources = sheets

    # first item in joined row is the key tuple from the first sheet.
    # first columns are the key columns from the first sheet, using its row (0)
    self.columns = []
    for i, c in enumerate(sheets[0].keyCols):
        self.addColumn(copy(c))
    self.setKeys(self.columns)

    for i, c in enumerate(sheets[0].nonKeyVisibleCols):
        self.addColumn(copy(c))

    for sheetnum, vs in enumerate(sheets[1:]):
        # subsequent elements are the rows from each source, in order of the source sheets
        ctr = collections.Counter(c.name for c in vs.nonKeyVisibleCols)
        for c in vs.nonKeyVisibleCols:
            newname = '%s_%s' % (vs.name, c.name)
            newcol = ExtendedColumn(newname, sheetnum=sheetnum+1, sourceCol=c)
            self.addColumn(newcol)

    self.rowsBySheetKey = {}  # [srcSheet][key] -> list(rowobjs from sheets[0])
    rowsByKey = {}  # [key] -> [key, rows0, rows1, ...]

    groupRowsByKey(sheets, self.rowsBySheetKey, rowsByKey)

    self.rows = []

    with Progress(gerund='joining', total=len(rowsByKey)) as prog:
        for k, combinedRows in rowsByKey.items():
            prog.addProgress(1)
            for combinedRow in combinedRows:
                if combinedRow[1]:
                    self.addRow(combinedRow[1])


## for SheetConcat
class ColumnConcat(Column):
    def __init__(self, name, cols, **kwargs):
        super().__init__(name, **kwargs)
        self.cols = cols

    def getColBySheet(self, s):
        for c in self.cols:
            if c.sheet is s:
                return c

    def calcValue(self, row):
        srcSheet, srcRow = row
        srcCol = self.getColBySheet(srcSheet)
        if srcCol:
            return srcCol.calcValue(srcRow)

    def setValue(self, row, v):
        srcSheet, srcRow = row
        srcCol = self.getColBySheet(srcSheet)
        if srcCol:
            srcCol.setValue(srcRow, v)
        else:
            fail('column not on source sheet')


# rowdef: (srcSheet, srcRow)
class SheetConcat(Sheet):
    'combination of multiple sheets by row concatenation. sourceCols=list(cols). '
    @asyncthread
    def reload(self):
        self.rows = []
        sourceSheets = []
        for cols in self.sourceCols:
            for c in cols:
                if c.sheet not in sourceSheets:
                    sourceSheets.append(c.sheet)

        self.columns = []
        self.addColumn(ColumnItem('origin_sheet', 0, width=0))
        for cols in self.sourceCols:
            self.addColumn(ColumnConcat(cols[0].name, cols, type=cols[0].type))

        for sheet in sourceSheets:
            for r in Progress(sheet.rows):
                self.addRow((sheet, r))

"""Relationship checks between two tables: key uniqueness, orphans, cardinality."""

import os

from .common import DataMedicError, pct
from .loader import load_table


def _split(spec):
    if ":" not in spec or spec.rfind(":") < 2:
        raise DataMedicError(f"Use FILE:COLUMN, got '{spec}'")
    path, col = spec.rsplit(":", 1)
    return path, col


def relate(parent_spec, child_spec, sheet_p=None, sheet_c=None):
    pp, pk = _split(parent_spec)
    cp, fk = _split(child_spec)
    if "::" in pp:
        pp, tp = pp.split("::", 1)
    else:
        tp = None
    if "::" in cp:
        cp, tc = cp.split("::", 1)
    else:
        tc = None
    P, _ = load_table(pp, sheet=sheet_p, table=tp)
    C, _ = load_table(cp, sheet=sheet_c, table=tc)
    for df, col, name in ((P, pk, pp), (C, fk, cp)):
        if col not in df.columns:
            raise DataMedicError(f"Column '{col}' not in {os.path.basename(name)}. Columns: {', '.join(df.columns[1:])}")
    pkeys = P[pk].astype(str).str.strip()
    ckeys = C[fk].astype(str).str.strip()
    pkeys_nb, ckeys_nb = pkeys[pkeys != ""], ckeys[ckeys != ""]
    dup = pkeys_nb[pkeys_nb.duplicated(keep=False)]
    pset = set(pkeys_nb)
    orphan = ckeys_nb[~ckeys_nb.isin(pset)]
    unused = pset - set(ckeys_nb)
    per_parent = ckeys_nb.value_counts()
    card = "one-to-one" if len(per_parent) and per_parent.max() == 1 else "one-to-many"
    if len(dup):
        card = "many-to-many (parent key is not unique)"
    # also compare after case/space normalisation to catch formatting-only mismatches
    norm_p = {k.casefold().replace(" ", "") for k in pset}
    fixable = orphan[orphan.map(lambda k: k.casefold().replace(" ", "") in norm_p)]
    return {
        "parent": {"file": os.path.basename(pp), "key": pk, "rows": int(len(P)), "blank_keys": int((pkeys == "").sum()),
                   "duplicate_key_rows": int(len(dup)), "duplicate_key_values": int(dup.nunique()),
                   "key_is_unique": len(dup) == 0},
        "child": {"file": os.path.basename(cp), "foreign_key": fk, "rows": int(len(C)), "blank_keys": int((ckeys == "").sum()),
                  "orphan_rows": int(len(orphan)), "orphan_pct": pct(len(orphan), len(ckeys_nb)),
                  "orphan_examples": list(orphan.value_counts().index[:10]),
                  "orphans_matching_after_case_or_space_fix": int(len(fixable))},
        "parent_keys_without_children": len(unused),
        "cardinality": card,
        "max_children_per_parent": int(per_parent.max()) if len(per_parent) else 0,
        "advice": "Orphaned rows are kept, not deleted: they may point to records missing from the parent extract. "
                  "Formatting-only mismatches can be fixed with a standardisation step if the keys are confirmed equal.",
    }

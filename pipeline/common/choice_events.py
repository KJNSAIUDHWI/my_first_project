"""Choice row projection shared by review, training and selection."""
def project(stem, manifest_rows, event_rows):
    base = {}
    for r in manifest_rows:
        base[int(r["row"])] = {"stem": stem, "row": int(r["row"]), "no": int(r["no"]),
                               "n_opts": int(r["n_opts"]),
                               "file": "%s_%03d.png" % (stem, int(r["row"])),
                               "answer": "", "pred_answer": "", "draft": "", "note": "",
                               "draft_note": "", "reviewed": False, "image_sha256": None,
                               "conf": 0.0}
    for e in event_rows:
        t = e.get("e")
        row = int(e.get("row") or 0)
        if t == "pred":
            for r in e.get("rows", []):
                rr = int(r["row"])
                if rr not in base:
                    continue
                a = r.get("answer", "")
                base[rr].update({"answer": a, "pred_answer": a, "draft": a,
                                 "reviewed": False, "image_sha256": None,
                                 "note": "", "draft_note": ""})
        elif t == "save" and row in base:
            old_draft = base[row]["draft"]
            if "answer" in e:
                base[row]["draft"] = e.get("answer", "")
            if "note" in e:
                base[row]["draft_note"] = (e.get("note") or "").strip()
            if base[row]["draft"] != base[row]["answer"] or base[row]["draft_note"]:
                base[row]["reviewed"] = False
                base[row]["image_sha256"] = None
            elif base[row]["draft"] != old_draft:
                # A draft-only change that returns to the already reviewed answer does
                # not re-open a review that a previous save had invalidated.
                base[row]["image_sha256"] = None
        elif t == "review" and row in base:
            a = e.get("answer", base[row]["draft"])
            base[row].update({"answer": a, "draft": a, "reviewed": True,
                              "note": "", "draft_note": "",
                              "image_sha256": e.get("image_sha256")})
        elif t == "special" and row in base:
            n = (e.get("note") or e.get("kind") or "特殊").strip()
            a = e.get("answer", base[row]["draft"])
            base[row].update({"answer": a, "draft": a, "reviewed": True,
                              "note": n, "draft_note": "",
                              "image_sha256": e.get("image_sha256")})
        elif t == "unreview" and row in base:
            base[row].update({"answer": base[row]["pred_answer"],
                              "draft": base[row]["pred_answer"],
                              "reviewed": False, "image_sha256": None,
                              "note": "", "draft_note": ""})
    return list(base.values())


#!/usr/bin/env python3
"""
restore_set_order.py

Restores the original custom (drag-and-drop) card order in an MSE 2.6 `.mse-set`
file after MSE 2.6's automatic `update cards script` migration moves upgraded
cards (Legendaries with Vorthos Box text, Planeswalkers, Sagas/Galleries,
split DFCs/Melds, and Tokens) to the end of the set.

Usage:
  1. Two-file mode (pre-update set + post-update set):
       python restore_set_order.py "OldSet.mse-set" "UpdatedSet.mse-set" [Output.mse-set]
     (If Output.mse-set is omitted, UpdatedSet.mse-set is updated in-place.)

  2. Single-file mode (before & after opening in MSE 2.6):
       Step A (before opening in MSE 2.6):
         python restore_set_order.py "MySet.mse-set"
         -> Creates "MySet.pre26-backup.mse-set" to record the original order.
       Step B (after opening "MySet.mse-set" in MSE 2.6 and saving):
         python restore_set_order.py "MySet.mse-set"
         -> Restores the original card order from "MySet.pre26-backup.mse-set"
            into "MySet.mse-set".
"""

import os
import shutil
import sys
import zipfile
from typing import Dict, List, Optional, Tuple


def parse_set_text(text: str) -> Tuple[str, List[str], str]:
    """
    Splits an MSE `set` file into:
      - prefix: header lines before the first `card:` block
      - cards: list of raw `card:` block strings (preserving exact line endings)
      - suffix: footer lines after the last `card:` block
    """
    lines = text.splitlines(keepends=True)
    top_indices: List[int] = []
    for i, line in enumerate(lines):
        check = line.lstrip("\ufeff")
        if check and not check[0].isspace() and not check.startswith("#"):
            top_indices.append(i)

    card_ranges: List[Tuple[int, int]] = []
    non_card_between: List[str] = []
    for idx, start in enumerate(top_indices):
        end = top_indices[idx + 1] if idx + 1 < len(top_indices) else len(lines)
        key = lines[start].lstrip("\ufeff").strip()
        if key == "card:":
            card_ranges.append((start, end))
        elif card_ranges:
            # Top-level block appearing after the first card: block
            non_card_between.append((start, end))

    if not card_ranges:
        raise ValueError("No 'card:' blocks found in set file.")

    first_card_start = card_ranges[0][0]
    prefix = "".join(lines[:first_card_start])
    cards = ["".join(lines[s:e]) for s, e in card_ranges]
    suffix = "".join("".join(lines[s:e]) for s, e in non_card_between)
    return prefix, cards, suffix


def extract_card_fields(card_block: str) -> Dict[str, str]:
    """Extracts 1-tab-indented key: value pairs from a `card:` block."""
    fields: Dict[str, str] = {}
    for line in card_block.splitlines():
        if line.startswith("\t") and not line.startswith("\t\t") and ":" in line:
            k, v = line[1:].split(":", 1)
            fields[k.strip()] = v.strip()
    return fields


def read_set_from_archive(mse_set_path: str) -> str:
    """Reads the `set` text file from inside a `.mse-set` zip archive."""
    with zipfile.ZipFile(mse_set_path, "r") as zf:
        if "set" not in zf.namelist():
            raise ValueError(f"'{mse_set_path}' is not a valid .mse-set archive (missing 'set' member).")
        return zf.read("set").decode("utf-8")


def get_mse_version(set_text: str) -> str:
    """Returns the top-level `mse_version` string from a `set` file."""
    for line in set_text.splitlines()[:10]:
        clean = line.lstrip("\ufeff").strip()
        if clean.startswith("mse_version:"):
            return clean.split(":", 1)[1].strip()
    return ""


def fix_split_back_planeswalker(pre_card_block: str, back_card_block: str) -> str:
    """
    Fixes an MSE 2.6 DFC migration bug where pre-Oct-2024 `m15-mainframe-dfc`
    cards stored back-face Planeswalker loyalty costs/abilities in
    `loyalty_cost_5..8` and `level_5_text..level_8_text` (offset +4),
    whereas MSE 2.6's `magic-m15-mainframe-dfc` update script looks in
    `loyalty_cost_9..16` and `level_9_text..level_16_text` (offset +8),
    leaving the split back-face Planeswalker with blank loyalty abilities.
    """
    pre_fields = extract_card_fields(pre_card_block)
    back_fields = extract_card_fields(back_card_block)

    if "planeswalker" not in back_fields.get("stylesheet", ""):
        return back_card_block

    # Check if back_card is missing loyalty/level text while pre_card had them at 5..8
    has_back_text = any(
        back_fields.get(f"level_{i}_text", "") or back_fields.get(f"loyalty_cost_{i}", "")
        for i in range(1, 5)
    )
    has_pre_5_to_8 = any(
        pre_fields.get(f"level_{i}_text", "") or pre_fields.get(f"loyalty_cost_{i}", "")
        for i in range(5, 9)
    )
    if has_back_text or not has_pre_5_to_8:
        return back_card_block

    insert_lines: List[str] = []
    for i in range(1, 5):
        lc = pre_fields.get(f"loyalty_cost_{i + 4}", "")
        if lc:
            insert_lines.append(f"\tloyalty_cost_{i}: {lc}\n")
    for i in range(1, 5):
        lt = pre_fields.get(f"level_{i + 4}_text", "")
        if lt:
            insert_lines.append(f"\tlevel_{i}_text: {lt}\n")

    if not insert_lines:
        return back_card_block

    # Insert right before `\tloyalty:` or `\tillustrator:` in back_card_block
    lines = back_card_block.splitlines(keepends=True)
    out_lines: List[str] = []
    inserted = False
    for line in lines:
        if not inserted and (line.startswith("\tloyalty:") or line.startswith("\tillustrator:")):
            out_lines.extend(insert_lines)
            inserted = True
        out_lines.append(line)
    if not inserted:
        out_lines.extend(insert_lines)
    return "".join(out_lines)


def reorder_cards(pre_cards: List[str], post_cards: List[str]) -> Tuple[List[str], dict]:
    """
    Reorders `post_cards` to match the original custom order of `pre_cards`.
    Handles:
      - Normal 1-to-1 cards (matched by name + time_created + identifying fields)
      - DFC cards split by MSE 2.6 into 2 linked cards (Front Face + Back Face)
      - Meld 3-in-1 cards split by MSE 2.6 into 3 linked cards
      - Duplicate-named cards (Basic Lands, Tokens) in exact relative order
      - Brand-new cards added in MSE 2.6 (kept attached to their preceding card)
    """
    pre_info = [extract_card_fields(c) for c in pre_cards]
    post_info = [extract_card_fields(c) for c in post_cards]

    uid_to_post_idx: Dict[str, int] = {}
    for idx, info in enumerate(post_info):
        uid = info.get("uid", "")
        if uid:
            uid_to_post_idx[uid] = idx

    used_post = [False] * len(post_cards)
    # Map from pre_idx -> list of post_idx that belong at pre_idx
    pre_to_post_indices: List[List[int]] = [[] for _ in range(len(pre_cards))]

    # Pass 1: Exact (name, time_created, image, illustrator, sub_type) match
    for pre_idx, p in enumerate(pre_info):
        p_name = p.get("name", "")
        p_time = p.get("time_created", "")
        p_img = p.get("image", "")
        p_ill = p.get("illustrator", "")
        p_sub = p.get("sub_type", "")
        for post_idx, q in enumerate(post_info):
            if used_post[post_idx]:
                continue
            if (
                q.get("name", "") == p_name
                and q.get("time_created", "") == p_time
                and q.get("image", "") == p_img
                and q.get("illustrator", "") == p_ill
                and q.get("sub_type", "") == p_sub
            ):
                used_post[post_idx] = True
                pre_to_post_indices[pre_idx].append(post_idx)
                break

    # Pass 2: Exact (name, time_created) match (for cards whose image/sub_type tags were updated)
    for pre_idx, p in enumerate(pre_info):
        if pre_to_post_indices[pre_idx]:
            continue
        p_name = p.get("name", "")
        p_time = p.get("time_created", "")
        for post_idx, q in enumerate(post_info):
            if used_post[post_idx]:
                continue
            if q.get("name", "") == p_name and q.get("time_created", "") == p_time:
                used_post[post_idx] = True
                pre_to_post_indices[pre_idx].append(post_idx)
                break

    # Pass 3: Split DFCs (1 pre card -> 2 linked post cards) and Meld 3-in-1 (1 pre card -> 3 linked post cards)
    split_count = 0
    for pre_idx, p in enumerate(pre_info):
        if pre_to_post_indices[pre_idx]:
            continue
        p_name1 = p.get("name", "")
        p_name2 = p.get("name_2", "")
        p_name3 = p.get("name_3", "")

        if p_name2:
            # Find front face matching name_1
            for post_idx, q in enumerate(post_info):
                if used_post[post_idx]:
                    continue
                if q.get("name", "") == p_name1:
                    group = [post_idx]
                    used_post[post_idx] = True

                    # Check linked back face / meld faces first
                    linked_uid_1 = q.get("linked_card_1", "")
                    linked_uid_2 = q.get("linked_card_2", "")
                    found_second = False
                    if linked_uid_1 in uid_to_post_idx:
                        b_idx = uid_to_post_idx[linked_uid_1]
                        if not used_post[b_idx]:
                            used_post[b_idx] = True
                            group.append(b_idx)
                            found_second = True
                    if not found_second:
                        # Fallback: look for unused card with name == p_name2
                        for b_idx, bq in enumerate(post_info):
                            if not used_post[b_idx] and bq.get("name", "") == p_name2:
                                used_post[b_idx] = True
                                group.append(b_idx)
                                break

                    if p_name3:
                        found_third = False
                        if linked_uid_2 in uid_to_post_idx:
                            c_idx = uid_to_post_idx[linked_uid_2]
                            if not used_post[c_idx]:
                                used_post[c_idx] = True
                                group.append(c_idx)
                                found_third = True
                        if not found_third:
                            for c_idx, cq in enumerate(post_info):
                                if not used_post[c_idx] and cq.get("name", "") == p_name3:
                                    used_post[c_idx] = True
                                    group.append(c_idx)
                                    break

                    if len(group) >= 2:
                        post_cards[group[1]] = fix_split_back_planeswalker(
                            pre_cards[pre_idx], post_cards[group[1]]
                        )

                    pre_to_post_indices[pre_idx].extend(group)
                    split_count += 1
                    break

    # Pass 4: Fallback match by (name, casting_cost, rarity) then (name) for any card whose time_created was reset
    for pre_idx, p in enumerate(pre_info):
        if pre_to_post_indices[pre_idx]:
            continue
        p_name = p.get("name", "")
        p_cc = p.get("casting_cost", "")
        p_rarity = p.get("rarity", "")
        matched = False
        for post_idx, q in enumerate(post_info):
            if not used_post[post_idx] and (
                q.get("name", "") == p_name
                and q.get("casting_cost", "") == p_cc
                and q.get("rarity", "") == p_rarity
            ):
                used_post[post_idx] = True
                pre_to_post_indices[pre_idx].append(post_idx)
                matched = True
                break
        if not matched:
            for post_idx, q in enumerate(post_info):
                if not used_post[post_idx] and q.get("name", "") == p_name:
                    used_post[post_idx] = True
                    pre_to_post_indices[pre_idx].append(post_idx)
                    break

    # Collect any brand-new cards in post_cards that did not exist in pre_cards
    # Attach each brand-new card after the post_card that immediately preceded it in post_cards
    extra_after_post: Dict[int, List[int]] = {}
    leading_extra: List[int] = []
    last_matched_post: Optional[int] = None
    for post_idx in range(len(post_cards)):
        if used_post[post_idx]:
            last_matched_post = post_idx
        else:
            if last_matched_post is None:
                leading_extra.append(post_idx)
            else:
                extra_after_post.setdefault(last_matched_post, []).append(post_idx)

    # Assemble final ordered list of post_card indices
    final_post_indices: List[int] = []
    final_post_indices.extend(leading_extra)

    def append_with_extras(p_idx: int) -> None:
        final_post_indices.append(p_idx)
        if p_idx in extra_after_post:
            for ext_idx in extra_after_post.pop(p_idx):
                append_with_extras(ext_idx)

    for group in pre_to_post_indices:
        for p_idx in group:
            append_with_extras(p_idx)

    # Safety check: ensure every single post_card is included exactly once
    if len(final_post_indices) != len(post_cards) or set(final_post_indices) != set(range(len(post_cards))):
        raise RuntimeError(
            f"Internal check failed: expected {len(post_cards)} cards, got {len(final_post_indices)}."
        )

    unmatched_pre = sum(1 for g in pre_to_post_indices if not g)
    new_cards_added = sum(1 for u in used_post if not u)
    moved_cards = sum(1 for i, idx in enumerate(final_post_indices) if i != idx)

    reordered = [post_cards[idx] for idx in final_post_indices]
    stats = {
        "pre_count": len(pre_cards),
        "post_count": len(post_cards),
        "matched_pre": len(pre_cards) - unmatched_pre,
        "unmatched_pre": unmatched_pre,
        "split_cards": split_count,
        "new_cards_in_post": new_cards_added,
        "positions_changed": moved_cards,
    }
    return reordered, stats


def write_reordered_archive(
    post_archive_path: str,
    output_archive_path: str,
    new_set_text: str,
) -> None:
    """
    Writes a new `.mse-set` zip archive containing all members of `post_archive_path`
    with `set` replaced by `new_set_text`.
    """
    temp_out = output_archive_path + ".tmp-rebuild"
    try:
        with zipfile.ZipFile(post_archive_path, "r") as zin:
            with zipfile.ZipFile(temp_out, "w", compression=zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    if item.filename == "set":
                        zout.writestr(item, new_set_text.encode("utf-8"))
                    else:
                        zout.writestr(item, zin.read(item.filename))
        os.replace(temp_out, output_archive_path)
    finally:
        if os.path.exists(temp_out):
            os.remove(temp_out)


def restore_order(pre_path: str, post_path: str, output_path: Optional[str] = None) -> dict:
    """Restores the card order of `pre_path` into `post_path` (writing to `output_path` or in-place)."""
    if output_path is None:
        output_path = post_path

    pre_text = read_set_from_archive(pre_path)
    post_text = read_set_from_archive(post_path)

    _, pre_cards, _ = parse_set_text(pre_text)
    post_prefix, post_cards, post_suffix = parse_set_text(post_text)

    reordered_cards, stats = reorder_cards(pre_cards, post_cards)
    new_set_text = post_prefix + "".join(reordered_cards) + post_suffix

    write_reordered_archive(post_path, output_path, new_set_text)
    return stats


def main(argv: List[str]) -> int:
    args = argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0

    if len(args) == 1:
        target_path = os.path.abspath(args[0])
        if not os.path.isfile(target_path):
            print(f"Error: File not found: {target_path}", file=sys.stderr)
            return 1

        base, ext = os.path.splitext(target_path)
        backup_path = f"{base}.pre26-backup{ext}"

        if not os.path.exists(backup_path):
            shutil.copy2(target_path, backup_path)
            print(f"Created pre-update backup: {backup_path}")
            print("Next steps:")
            print(f"  1. Open '{os.path.basename(target_path)}' in MSE 2.6 and save it (Ctrl+S).")
            print(f"  2. Run this script again with '{os.path.basename(target_path)}' to restore the custom order.")
            return 0
        else:
            print(f"Found backup: {backup_path}")
            print(f"Restoring custom card order into: {target_path} ...")
            stats = restore_order(backup_path, target_path, target_path)
            print(
                f"Done! Matched {stats['matched_pre']}/{stats['pre_count']} original cards "
                f"({stats['split_cards']} split DFC/Meld, {stats['new_cards_in_post']} new card(s)) "
                f"-> {stats['post_count']} total cards in '{os.path.basename(target_path)}'."
            )
            return 0

    elif len(args) in (2, 3):
        pre_path = os.path.abspath(args[0])
        post_path = os.path.abspath(args[1])
        out_path = os.path.abspath(args[2]) if len(args) == 3 else post_path

        for p in (pre_path, post_path):
            if not os.path.isfile(p):
                print(f"Error: File not found: {p}", file=sys.stderr)
                return 1

        # If the user accidentally passed (post_2.6, pre_2.0) in reverse order and no 3rd arg,
        # warn and auto-detect based on mse_version if one is 2.0.x and the other is 2.6.x.
        pre_ver = get_mse_version(read_set_from_archive(pre_path))
        post_ver = get_mse_version(read_set_from_archive(post_path))
        if pre_ver.startswith("2.6") and not post_ver.startswith("2.6") and len(args) == 2:
            print(
                f"Note: Swapping arguments so '{os.path.basename(post_path)}' (MSE {post_ver}) "
                f"is the order reference and '{os.path.basename(pre_path)}' (MSE {pre_ver}) is updated."
            )
            pre_path, post_path = post_path, pre_path
            out_path = post_path

        stats = restore_order(pre_path, post_path, out_path)
        print(
            f"Successfully restored card order from '{os.path.basename(pre_path)}' "
            f"into '{os.path.basename(out_path)}'!"
        )
        print(
            f"  Original cards matched: {stats['matched_pre']}/{stats['pre_count']}\n"
            f"  Split DFC/Meld cards:   {stats['split_cards']}\n"
            f"  New cards preserved:    {stats['new_cards_in_post']}\n"
            f"  Total cards written:    {stats['post_count']}"
        )
        return 0

    else:
        print("Error: Expected 1, 2, or 3 file arguments.", file=sys.stderr)
        print(__doc__.strip(), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

# ParkingBreaker source review

Refresh the Desktop Markdown and its four numbered parts from a clean Git checkout. Run this command at the repository root after verifying the intended commit on GitHub.

```bash
python3 tools/source-review/update_source_review.py \
  --repo "$PWD" \
  --previous "$HOME/Desktop/ParkingBreaker source.md" \
  --out "$HOME/Desktop/ParkingBreaker source.md" \
  --parts 4 --apply
```

The previous export supplies the curated file classifications and substitutions. If the full file is missing, the generator reads its numbered sibling parts in order. It verifies the seed manifest against the original Git tree and checks the size and SHA-256 of each included body. A damaged seed stops the refresh. A verified copy of the earlier full export can also be passed to `--previous`.

The new manifest identifies every tracked path and Git blob. Verbatim bodies preserve the committed bytes, including line endings. Filtered bodies retain curated substitutions and use best-effort masking for changed content. Binaries, historical reports and selected run outputs have an omission reason in the manifest. Git remains the source for any omitted or filtered content.

The four parts are consecutive UTF-8 slices of the full file. A boundary may split a line. Final blank lines after all source bodies make the full byte count divisible by the requested number of parts. A cut moves slightly if it would split a multi-byte character. Concatenating the parts in numeric order reproduces the full file. For example, on the Desktop run the following command.

```bash
cat "ParkingBreaker source part "{1,2,3,4}.md | cmp - "ParkingBreaker source.md"
```

Without `--apply`, use a separate `--out` path to preview the full file. An applied refresh stages all outputs before replacement, backs up the previous files under `~/workstation-backups/pb-source-review/old-*`, and prints the backup location and part sizes. Restore by copying that backup's files to the original destination. If the backup contains only parts, those parts also serve as a seed for the next refresh.

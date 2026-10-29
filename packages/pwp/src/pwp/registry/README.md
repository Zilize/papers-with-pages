# Institution registry

`institutions.json` is a curated list of well-known AI/CS research institutions:
universities, industrial labs, and research institutes. Load it with
`pwp.institutions.load_registry()`. The registry holds names as written; normalization
and lookup tables are built by `pwp.institutions.InstitutionIndex`, and
`pwp.match_institutions()` matches a resolved paper against it.

## Fields

| Field | Meaning |
| --- | --- |
| `id` | Stable short slug used as the key and as `parent` target. Never reuse a removed id. |
| `ror` | Full ROR URL, or `null` when ROR has no record for exactly this unit. |
| `names` | Full names for case-insensitive matching. The first entry is the canonical English name, followed by the Chinese name, common forms, and lab or department names that are not separate entries (for example "MIT CSAIL"). ROR labels and aliases are included, but not names with a country suffix such as "Google (United States)". |
| `acronyms` | Short forms for exact, case-sensitive token matching only. Never use them for substring search. |
| `domains` | Bare hosts the institution owns, without `www.`. Matchers should compare host suffixes. Shared hosting domains such as `github.io` are never listed. |
| `github_orgs` | GitHub organization logins owned by the institution or one of its labs. |
| `parent` | `id` of the containing institution, for example `google-deepmind` → `google`, `fair` → `meta`, `casia` → `cas`. |
| `country` | ISO 3166-1 alpha-2 code for the unit's headquarters. |

Each name, acronym, domain, and GitHub org belongs to at most one entry. A subdomain
can belong to a different entry only when that entry descends from the domain's owner,
for example `ai.meta.com` (`fair`) under `meta.com` (`meta`). A matcher can therefore
pick the most specific match and roll it up through `parent`.

## Inclusion

- Universities and institutes that publish regularly at top AI/CS venues, including
  leading universities in the US, China, Hong Kong, Taiwan, the UK, Europe, Canada,
  Japan, Korea, Singapore, Israel, Australia, the Middle East, and India.
- Companies and labs that publish widely cited AI research or major open models.
- A lab becomes its own entry when papers commonly credit it apart from its parent
  and it has its own domain, GitHub org, or ROR record. Examples are Google DeepMind,
  FAIR, MSRA, DAMO, and Tencent ARC. University labs and departments are usually
  `names` on the university entry instead.

## Verification rules

- `ror` comes from the ROR API (`api.ror.org/v2/organizations`). The record's name and
  country must match the institution, and its status must be active.
- `domains` are the ROR `domains`, plus hosts that are either the ROR website host or
  a homepage whose fetched title names the institution.
- `github_orgs` were checked with `gh api users/<login>`. Each must be an
  `Organization` account whose name, blog, email, bio, or location ties it to the
  institution. Personal accounts and orgs with empty or unrelated profiles are left out.
- An acronym is dropped when it is ambiguous with another well-known institution
  (UW, NTU, UVA, OSU, PSU, UCD, NEU, GT). ROR acronyms of one or two letters are
  also dropped.
- A full name shared by two real institutions is listed only with a qualifier. For
  example, "Northeastern University" is also a university in Shenyang, so the entry
  lists "Northeastern University, Boston" instead.
- A short form written with brackets, such as "HKUST (GZ)", is a name rather than an
  acronym, so it matches as a whole before brackets are split.
- If a field cannot be verified, leave it empty instead of guessing.

## Updating

1. Edit `institutions.json`. Keep the order: grouped by region, then industry.
2. Verify every new ROR ID, domain, and GitHub org using the rules above.
3. Bump `updated`. Bump `version` only when the file layout changes.
4. Run `pytest tests/test_institutions.py`. It checks id uniqueness, ROR URL format,
   collisions across entries, domain hygiene, and parent references.

Source Provenance Verification
===============================

.. currentmodule:: packaging.source

Offline, read-only verification that the ``direct_url`` data recorded for an
installed package proves it came from an approved source or archive. The
verification is based solely on the immutable evidence carried by
:pep:`610` data: it never accesses the network, installs or imports a package,
and never mutates its inputs.

.. versionadded:: 26.4

Concepts
--------

:func:`verify_source` takes an *approved requirement* (the direct reference
the supply-chain policy allows) and the *installed* ``direct_url`` data, and
returns a :class:`VerificationResult` with one of three verdicts:

* :attr:`~VerificationStatus.MATCH` — immutable evidence proves identity.
* :attr:`~VerificationStatus.MISMATCH` — recorded evidence contradicts the
  requirement.
* :attr:`~VerificationStatus.INSUFFICIENT_EVIDENCE` — the evidence is
  compatible but not immutable enough; ``missing`` names the fields that would
  be needed for a stronger conclusion.

Immutable versus movable evidence matters:

* For VCS sources, only ``commit_id`` is immutable. A matching URL plus a
  matching branch or tag (``requested_revision``) cannot prove that the same
  revision was installed, because branches and tags move.
* For archives, trusted digest constraints in ``archive_info.hashes`` are the
  proof. When several algorithms are pinned, every required algorithm must be
  present and match; one conflict is a mismatch, one missing algorithm is
  insufficient evidence.
* Local directories, including editable installs, are never an immutable
  artifact: an identical path can hold different content at any time, so they
  always yield insufficient evidence.

URLs are compared after lower-casing the scheme and host, removing
credentials, canonicalizing percent-encoding, and normalizing the
``subdirectory`` field. Project names are normalized with
:func:`~packaging.utils.canonicalize_name`. Different repositories, different
subdirectories, and different commits are never merged together, and
credentials never appear in result messages.

Offline examples
----------------

.. code-block:: python

    from packaging.source import VerificationStatus, verify_source

    # 1. A pinned immutable commit matches, even if the recorded URL carries
    #    credentials or uses a git+ scheme prefix.
    approved = {
        "url": "https://git.example.org/owner/repo.git",
        "vcs_info": {
            "vcs": "git",
            "commit_id": "2df7bdd8dfef7b879390b9fc4016f02af2f118d4",
            "requested_revision": "main",
        },
    }
    installed = {
        "url": "https://git.example.org/owner/repo.git",
        "vcs_info": {
            "vcs": "git",
            "commit_id": "2df7bdd8dfef7b879390b9fc4016f02af2f118d4",
            "requested_revision": "main",
        },
    }
    result = verify_source(approved, installed)
    assert result.status is VerificationStatus.MATCH
    # Evidence compared: URL, subdirectory, VCS, immutable commit_id (the
    # movable requested_revision is reported but does not decide the verdict).

    # 2. Same branch name, different commit: a mismatch.
    other_commit = {
        "url": "https://git.example.org/owner/repo.git",
        "vcs_info": {
            "vcs": "git",
            "commit_id": "0000000000000000000000000000000000000000",
            "requested_revision": "main",
        },
    }
    result = verify_source(approved, other_commit)
    assert result.status is VerificationStatus.MISMATCH

    # 3. Correct archive URL but a different digest: a mismatch.
    approved_archive = {
        "url": "https://files.example.org/pkg-1.0.tar.gz",
        "archive_info": {"hashes": {"sha256": "f" * 64}},
    }
    other_archive = {
        "url": "https://files.example.org/pkg-1.0.tar.gz",
        "archive_info": {"hashes": {"sha256": "e" * 64}},
    }
    result = verify_source(approved_archive, other_archive)
    assert result.status is VerificationStatus.MISMATCH
    # Evidence compared: URL, subdirectory, archive_hash (sha256 conflict).

    # 4. An editable local directory: never an immutable match.
    editable = {
        "url": "file:///srv/local/pkg",
        "dir_info": {"editable": True},
    }
    result = verify_source(editable, editable)
    assert result.status is VerificationStatus.INSUFFICIENT_EVIDENCE
    # missing reports that a commit_id or an archive digest would be needed.

Each :class:`Evidence` entry in ``result.evidence`` records the
:class:`EvidenceType` actually compared, whether it matched, and a
credential-free detail string, so an auditor can explain every decision.
Whether the package can be imported on the current machine is irrelevant to
the verdict.

Reference
---------

.. autofunction:: verify_source

.. autoclass:: VerificationResult
    :members:
    :exclude-members: __init__, __new__

.. autoclass:: Evidence
    :members:
    :exclude-members: __init__, __new__

.. autoclass:: VerificationStatus
    :members:
    :exclude-members: __init__, __new__

.. autoclass:: SourceKind
    :members:
    :exclude-members: __init__, __new__

.. autoclass:: EvidenceType
    :members:
    :exclude-members: __init__, __new__

.. autoexception:: SourceVerificationError
    :exclude-members: __init__, __new__

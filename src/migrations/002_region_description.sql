-- v2: a region can carry a written description of what was observed.
--
-- Added for the vision-model proposer, which returns a sentence per region
-- ("hairline crack running diagonally from the bearing seat"). The classical
-- baseline leaves it NULL. Additive only: no existing column changes, so every
-- region, citation and sign-off recorded under v1 is untouched.
ALTER TABLE image_regions ADD COLUMN description TEXT;

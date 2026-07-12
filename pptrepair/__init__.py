"""pptrepair — diagnose and repair PowerPoint files corrupted on OneDrive.

``pptrepair check`` classifies .pptx files as intact or as one of the
known OneDrive corruption patterns; ``pptrepair repair`` rebuilds an
openable presentation from the surviving data, or salvages the
surviving content into a recovery folder when the slides themselves
are gone.
"""

__version__ = "1.0.0"

from .base import TCGStrategy
from .local import LocalStrategy
from .lorcana import LorcanaStrategy
from .mtg import MTGStrategy
from .pokemon import PokemonStrategy

__all__ = ["LocalStrategy", "LorcanaStrategy", "MTGStrategy", "PokemonStrategy", "TCGStrategy"]
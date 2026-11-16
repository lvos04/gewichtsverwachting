"""Koppelsleutels voor de alleen-lezen Bedrijfskoppeling API v1 (alleen SHA-256-hash)."""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('koppelsleutels',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('naam', sa.String(length=100), nullable=False),
    sa.Column('voorvoegsel', sa.String(length=8), nullable=False),
    sa.Column('sleutel_hash', sa.String(length=64), nullable=False),
    sa.Column('aangemaakt_op', sa.DateTime(), nullable=False),
    sa.Column('aangemaakt_door', sa.Integer(), nullable=True),
    sa.Column('laatst_gebruikt_op', sa.DateTime(), nullable=True),
    sa.Column('ingetrokken_op', sa.DateTime(), nullable=True),
    sa.Column('ingetrokken_door', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['aangemaakt_door'], ['users.id'], ),
    sa.ForeignKeyConstraint(['ingetrokken_door'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_koppelsleutels_sleutel_hash'), 'koppelsleutels', ['sleutel_hash'], unique=True)


def downgrade():
    op.drop_index(op.f('ix_koppelsleutels_sleutel_hash'), table_name='koppelsleutels')
    op.drop_table('koppelsleutels')

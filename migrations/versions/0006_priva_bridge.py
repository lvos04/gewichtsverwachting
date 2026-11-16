"""Optionele Priva Bridge: synchronisaties, dagwaardencache met herkomst en versleuteld token.

Alleen nieuwe tabellen; bestaande gegevens en berekeningen blijven ongewijzigd. De koppeling
staat standaard uit (instelling ``bridge_enabled``).
"""
from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('bridge_syncs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('trigger', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('period_from', sa.String(length=10), nullable=True),
    sa.Column('period_to', sa.String(length=10), nullable=True),
    sa.Column('pages', sa.Integer(), nullable=False),
    sa.Column('rows_received', sa.Integer(), nullable=False),
    sa.Column('rows_stored', sa.Integer(), nullable=False),
    sa.Column('error_code', sa.String(length=40), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('retry_after', sa.DateTime(), nullable=True),
    sa.Column('details_json', sa.Text(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_bridge_syncs_started_at'), 'bridge_syncs', ['started_at'], unique=False)
    op.create_table('bridge_daily_values',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('sync_id', sa.Integer(), nullable=False),
    sa.Column('scope', sa.String(length=40), nullable=False),
    sa.Column('local_date', sa.String(length=10), nullable=False),
    sa.Column('metric', sa.String(length=80), nullable=False),
    sa.Column('value', sa.Float(), nullable=True),
    sa.Column('unit', sa.String(length=40), nullable=True),
    sa.Column('aggregation', sa.String(length=20), nullable=True),
    sa.Column('coverage', sa.Float(), nullable=True),
    sa.Column('quality', sa.String(length=20), nullable=False),
    sa.Column('resolved_from', sa.String(length=40), nullable=True),
    sa.Column('sources_json', sa.Text(), nullable=False),
    sa.Column('import_id', sa.String(length=80), nullable=True),
    sa.Column('revision', sa.Integer(), nullable=True),
    sa.Column('fetched_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['sync_id'], ['bridge_syncs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_bridge_daily_lookup', 'bridge_daily_values', ['scope', 'local_date', 'metric'], unique=False)
    op.create_index(op.f('ix_bridge_daily_values_fetched_at'), 'bridge_daily_values', ['fetched_at'], unique=False)
    op.create_index(op.f('ix_bridge_daily_values_sync_id'), 'bridge_daily_values', ['sync_id'], unique=False)
    op.create_table('bridge_credentials',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('ciphertext', sa.Text(), nullable=False),
    sa.Column('set_at', sa.DateTime(), nullable=False),
    sa.Column('set_by', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['set_by'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade():
    op.drop_table('bridge_credentials')
    op.drop_index(op.f('ix_bridge_daily_values_sync_id'), table_name='bridge_daily_values')
    op.drop_index(op.f('ix_bridge_daily_values_fetched_at'), table_name='bridge_daily_values')
    op.drop_index('ix_bridge_daily_lookup', table_name='bridge_daily_values')
    op.drop_table('bridge_daily_values')
    op.drop_index(op.f('ix_bridge_syncs_started_at'), table_name='bridge_syncs')
    op.drop_table('bridge_syncs')

"""Optionele TOTP-MFA per gebruiker en correcties van oogstregistraties met reden.

Alleen toevoegingen: bestaande gegevens blijven ongewijzigd. MFA staat voor alle
bestaande accounts uit; bestaande oogstrijen krijgen geen reden/auteur.
"""
from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users') as batch:
        batch.add_column(sa.Column('mfa_secret', sa.String(length=64), nullable=True))
        batch.add_column(sa.Column('mfa_enabled', sa.Boolean(), nullable=False, server_default='0'))
        batch.add_column(sa.Column('mfa_last_step', sa.Integer(), nullable=True))
    op.create_table('mfa_challenges',
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('token_hash')
    )
    op.create_index(op.f('ix_mfa_challenges_expires_at'), 'mfa_challenges', ['expires_at'], unique=False)
    with op.batch_alter_table('harvests') as batch:
        batch.add_column(sa.Column('supersedes_id', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('reason', sa.Text(), nullable=True))
        batch.add_column(sa.Column('author_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_harvests_supersedes_id_harvests', 'harvests', ['supersedes_id'], ['id'])
        batch.create_foreign_key('fk_harvests_author_id_users', 'users', ['author_id'], ['id'])


def downgrade():
    with op.batch_alter_table('harvests') as batch:
        batch.drop_constraint('fk_harvests_author_id_users', type_='foreignkey')
        batch.drop_constraint('fk_harvests_supersedes_id_harvests', type_='foreignkey')
        batch.drop_column('author_id')
        batch.drop_column('reason')
        batch.drop_column('supersedes_id')
    op.drop_index(op.f('ix_mfa_challenges_expires_at'), table_name='mfa_challenges')
    op.drop_table('mfa_challenges')
    with op.batch_alter_table('users') as batch:
        batch.drop_column('mfa_last_step')
        batch.drop_column('mfa_enabled')
        batch.drop_column('mfa_secret')
